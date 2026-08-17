"""Tests for the channel-aware app-repair SYSTEM prompt template.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §10.9.

This is Channel B's scaffolding. The prompt is composed at session_start
and lands in the bot's normal session loop so an in-situ Telegram /
Slack / Discord repair conversation can produce structured proposal
blocks parseable by the shared :mod:`app_repair_proposals` module.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

from app_repair_prompt import (  # noqa: E402
    APP_REPAIR_BEGIN_MARKER,
    APP_REPAIR_END_MARKER,
    AppRepairPromptContext,
    build_app_repair_system_prompt,
    wrap_for_session_prefix,
)
from app_repair_proposals import extract_proposals  # noqa: E402


# ── Determinism (same ctx in → same text out) ───────────────────────────────


def test_prompt_is_deterministic():
    ctx = AppRepairPromptContext(bot_id="bot-x")
    assert build_app_repair_system_prompt(ctx) == build_app_repair_system_prompt(ctx)


# ── Audience branches ──────────────────────────────────────────────────────


def test_primary_user_audience_line():
    prompt = build_app_repair_system_prompt(
        AppRepairPromptContext(bot_id="b", audience="primary_user"),
    )
    assert "primary user" in prompt.lower()
    # Primary-user audience: plain English first, structured second.
    assert "Use plain English" in prompt or "plain English first" in prompt


def test_pod_operator_audience_line():
    prompt = build_app_repair_system_prompt(
        AppRepairPromptContext(bot_id="b", audience="pod_operator"),
    )
    assert "operator" in prompt.lower()


def test_team_member_audience_blocks_proposals():
    """Team members can't approve repairs — the prompt explicitly tells
    the bot not to emit proposals for their requests."""
    prompt = build_app_repair_system_prompt(
        AppRepairPromptContext(bot_id="b", audience="team_member"),
    )
    assert "team member" in prompt.lower()
    assert "flag" in prompt.lower() or "primary" in prompt.lower()


# ── Channel branches ───────────────────────────────────────────────────────


def test_telegram_channel_uses_evo_file_proposal_apply_path():
    """Non-admin-UI channels route through `evo file-proposal`. The
    bot's bot_id is interpolated into the example command so the LLM
    can copy-paste it verbatim."""
    prompt = build_app_repair_system_prompt(
        AppRepairPromptContext(bot_id="team-bot-c", channel="telegram"),
    )
    assert "evo file-proposal" in prompt
    assert "--on-behalf-of team-bot-c" in prompt
    assert "Telegram" in prompt


def test_slack_channel_label_appears():
    prompt = build_app_repair_system_prompt(
        AppRepairPromptContext(bot_id="b", channel="slack"),
    )
    assert "Slack" in prompt


def test_admin_ui_channel_skips_evo_routing():
    """In Channel A (admin UI), proposals are clicked-to-apply directly
    — the bot doesn't need to invoke evo file-proposal."""
    prompt = build_app_repair_system_prompt(
        AppRepairPromptContext(bot_id="b", channel="admin_ui"),
    )
    assert "evo file-proposal" not in prompt
    assert "click-to-Apply" in prompt or "Apply chip" in prompt


# ── Output format (parseable by the shared parser) ─────────────────────────


def test_prompt_describes_correct_block_format():
    """The prompt MUST teach the exact <<<repair_proposal action="…">>>{...}<<<end>>>
    shape — that's the format both channels' parsers expect."""
    prompt = build_app_repair_system_prompt(
        AppRepairPromptContext(bot_id="b"),
    )
    assert '<<<repair_proposal action="ACTION_NAME">>>' in prompt
    assert "<<<end>>>" in prompt


def test_prompt_lists_every_valid_action():
    """All five actions must be documented; if we add a new action
    without updating the prompt the LLM can't be expected to know
    about it."""
    prompt = build_app_repair_system_prompt(
        AppRepairPromptContext(bot_id="b"),
    )
    for action in (
        "propose_field_edit",
        "propose_file_edit",
        "propose_test_exemption",
        "mark_resolved",
        "done",
    ):
        assert action in prompt, f"action {action} not described in prompt"


def test_synthetic_response_in_prompt_format_parses_cleanly():
    """End-to-end portability check: a bot response that follows the
    prompt's instructions parses through the shared extractor."""
    prompt = build_app_repair_system_prompt(
        AppRepairPromptContext(bot_id="b"),
    )
    # The example block format from the prompt is taught with a body
    # of {{"key": "value", ...}} (double braces because the template
    # uses .format()-style literal braces). Construct a realistic
    # response in the actual emitted shape.
    response = (
        "Sure, here's what I'd propose:\n\n"
        '<<<repair_proposal action="propose_field_edit">>>\n'
        '{"field": "description", "after": "updated text", '
        '"rationale": "matches new behavior"}\n'
        '<<<end>>>\n\n'
        "Want me to file it?"
    )
    cleaned, proposals = extract_proposals(response)
    assert len(proposals) == 1
    assert proposals[0].action == "propose_field_edit"
    assert proposals[0].payload["field"] == "description"
    assert "<<<repair_proposal" not in cleaned


# ── Marker block wrap ──────────────────────────────────────────────────────


def test_wrap_for_session_prefix_uses_known_markers():
    body = "system prompt body"
    wrapped = wrap_for_session_prefix(body)
    assert wrapped.startswith(APP_REPAIR_BEGIN_MARKER)
    assert wrapped.endswith(APP_REPAIR_END_MARKER)
    assert body in wrapped


def test_session_surface_loads_prompt_for_bot():
    """Session_surface's loader wraps the prompt with the bot's id."""
    from session_surface import load_app_repair_prompt_block
    block = load_app_repair_prompt_block(bot_id="my-bot", role="primary")
    assert block, "prompt block returned empty for a valid bot"
    assert "--on-behalf-of my-bot" in block


def test_session_surface_loader_handles_missing_bot_id():
    """No bot id → no block. The apply path needs --on-behalf-of, so
    emitting a generic prompt without it would mis-train the LLM."""
    from session_surface import load_app_repair_prompt_block
    assert load_app_repair_prompt_block(bot_id=None, role=None) == ""
    assert load_app_repair_prompt_block(bot_id="", role=None) == ""

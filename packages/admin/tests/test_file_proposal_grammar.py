"""Tests for the `evo file-proposal` command grammar.

Pure parsing — no I/O, no Proposal writes (those are exercised in
test_evo_file_proposal_handler.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.file_proposal_grammar import (  # noqa: E402
    KIND_FILE_PROPOSAL,
    KIND_USAGE_ERROR,
    parse_file_proposal,
)


# ── Happy path ──────────────────────────────────────────────────────────────


def test_full_command_parses():
    cmd = parse_file_proposal(
        '--on-behalf-of team-bot-c --app journal '
        '--action mark_resolved --content {"signature":"abc"}'
    )
    assert cmd.kind == KIND_FILE_PROPOSAL
    assert cmd.on_behalf_of == "team-bot-c"
    assert cmd.app_id == "journal"
    assert cmd.action == "mark_resolved"
    assert cmd.content == {"signature": "abc"}


def test_equals_form_parses():
    cmd = parse_file_proposal(
        '--on-behalf-of=team-bot-c --app=j --action=done --content={}'
    )
    assert cmd.kind == KIND_FILE_PROPOSAL
    assert cmd.action == "done"


def test_quoted_content_with_spaces_parses():
    cmd = parse_file_proposal(
        '--on-behalf-of s --app j --action propose_field_edit '
        '--content \'{"field": "description", "after": "new prose with spaces"}\''
    )
    assert cmd.kind == KIND_FILE_PROPOSAL
    assert cmd.content["after"] == "new prose with spaces"


# ── Missing flags ──────────────────────────────────────────────────────────


def test_missing_args_is_usage_error():
    cmd = parse_file_proposal("")
    assert cmd.kind == KIND_USAGE_ERROR


def test_missing_one_flag_is_usage_error():
    cmd = parse_file_proposal(
        "--on-behalf-of x --app y --action done"
        # no --content
    )
    assert cmd.kind == KIND_USAGE_ERROR
    assert "--content" in cmd.usage_error


def test_dangling_flag_without_value_is_usage_error():
    cmd = parse_file_proposal("--on-behalf-of")
    assert cmd.kind == KIND_USAGE_ERROR


# ── Validation ─────────────────────────────────────────────────────────────


def test_bad_bot_id_rejected():
    cmd = parse_file_proposal(
        '--on-behalf-of "with spaces" --app j --action done --content {}'
    )
    assert cmd.kind == KIND_USAGE_ERROR


def test_bad_app_id_rejected():
    cmd = parse_file_proposal(
        '--on-behalf-of s --app "../traversal" --action done --content {}'
    )
    assert cmd.kind == KIND_USAGE_ERROR


def test_unknown_action_rejected():
    cmd = parse_file_proposal(
        '--on-behalf-of s --app j --action wipe_disk --content {}'
    )
    assert cmd.kind == KIND_USAGE_ERROR
    assert "wipe_disk" in cmd.usage_error


def test_non_object_content_rejected():
    cmd = parse_file_proposal(
        '--on-behalf-of s --app j --action done --content [1,2,3]'
    )
    assert cmd.kind == KIND_USAGE_ERROR


def test_malformed_json_content_rejected():
    cmd = parse_file_proposal(
        '--on-behalf-of s --app j --action done --content {not_json}'
    )
    assert cmd.kind == KIND_USAGE_ERROR


# ── Positional token rejected ─────────────────────────────────────────────


def test_positional_token_rejected():
    cmd = parse_file_proposal(
        'team-bot-c --app j --action done --content {}'
    )
    assert cmd.kind == KIND_USAGE_ERROR


# ── Grammar's action vocabulary matches shared parser ─────────────────────


def test_grammar_action_set_matches_shared_parser():
    """The grammar's _VALID_ACTIONS must match the shared parser's set —
    if they drift, the bot can emit an action the handler can write but
    that no one can apply (or vice versa)."""
    from evolve_admin.applications import file_proposal_grammar as g
    sys.path.insert(0, str(_ADMIN_DIR.parent / "analyzer"))
    from app_repair_proposals import VALID_ACTIONS as SHARED
    assert g._VALID_ACTIONS == SHARED

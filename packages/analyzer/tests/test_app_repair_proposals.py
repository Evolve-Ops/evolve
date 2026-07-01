"""Shared parser for app-repair proposal blocks.

This module is the single source of truth for the
``<<<repair_proposal action="…">>>{...}<<<end>>>`` block format. Both
Channel A (admin-UI chat — :mod:`evolve_admin.applications.repair_chat`)
and Channel B (bot-side in-situ — :mod:`analyzer.app_repair_prompt`)
emit blocks of this shape, so any drift between the two would silently
break apply-route compatibility.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §10.9.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

from app_repair_proposals import (  # noqa: E402
    VALID_ACTIONS,
    extract_proposals,
    render_proposal_block,
)


# ── Empty / no-proposal text ────────────────────────────────────────────────


def test_empty_string_returns_no_proposals():
    cleaned, proposals = extract_proposals("")
    assert cleaned == ""
    assert proposals == []


def test_plain_text_returns_unchanged():
    text = "Here is some plain reply with no blocks."
    cleaned, proposals = extract_proposals(text)
    assert cleaned == text
    assert proposals == []


# ── Single proposal round-trip ──────────────────────────────────────────────


def test_single_field_edit_round_trips():
    payload = {
        "field": "success_criteria.observable_outcomes",
        "before": ["x"], "after": ["x", "y"],
        "rationale": "covers the new daily check",
    }
    block = render_proposal_block("propose_field_edit", payload)
    text = f"Here's my suggestion:\n\n{block}\n\nLet me know."
    cleaned, proposals = extract_proposals(text)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.action == "propose_field_edit"
    assert p.payload == payload
    assert "Here's my suggestion" in cleaned
    # Cleaned text must not retain the tags or the body.
    assert "<<<repair_proposal" not in cleaned
    assert "<<<end>>>" not in cleaned


def test_done_with_empty_payload_round_trips():
    block = render_proposal_block("done", {})
    cleaned, proposals = extract_proposals(f"All good. {block}")
    assert len(proposals) == 1
    assert proposals[0].action == "done"
    assert proposals[0].payload == {}


# ── Multiple proposals in one reply ─────────────────────────────────────────


def test_multiple_proposals_extract_in_order():
    b1 = render_proposal_block("propose_field_edit", {"field": "description",
                                                       "after": "new"})
    b2 = render_proposal_block("mark_resolved", {"signature": "abcd1234",
                                                  "rationale": "ok"})
    b3 = render_proposal_block("done", {})
    text = f"Step one:\n{b1}\nStep two:\n{b2}\nWrap up:\n{b3}"
    cleaned, proposals = extract_proposals(text)
    assert [p.action for p in proposals] == [
        "propose_field_edit", "mark_resolved", "done",
    ]
    # Each proposal has a unique id.
    ids = [p.id for p in proposals]
    assert len(set(ids)) == len(ids)
    # Each id is short hex.
    for pid in ids:
        assert len(pid) == 8
        int(pid, 16)


# ── Unknown action gets dropped ─────────────────────────────────────────────


def test_unknown_action_silently_dropped():
    text = (
        'before <<<repair_proposal action="reformat_universe">>>{}<<<end>>> '
        'after'
    )
    cleaned, proposals = extract_proposals(text)
    assert proposals == []
    # Unknown block stripped from cleaned output (no tags, no body).
    assert "reformat_universe" not in cleaned
    assert "<<<repair_proposal" not in cleaned


# ── Malformed JSON gets dropped ─────────────────────────────────────────────


def test_malformed_json_payload_dropped():
    text = (
        '<<<repair_proposal action="propose_field_edit">>>'
        '{"field": broken json '
        '<<<end>>>'
    )
    cleaned, proposals = extract_proposals(text)
    assert proposals == []


def test_non_object_payload_dropped():
    text = (
        '<<<repair_proposal action="propose_field_edit">>>'
        '["not", "a", "dict"]'
        '<<<end>>>'
    )
    cleaned, proposals = extract_proposals(text)
    assert proposals == []


# ── Unterminated block leaves tail in cleaned text ──────────────────────────


def test_unterminated_block_emits_tail_as_text():
    text = (
        'opening <<<repair_proposal action="propose_field_edit">>>'
        '{"field": "x"} (no close tag!)'
    )
    cleaned, proposals = extract_proposals(text)
    assert proposals == []
    # The tail (including the tag) survives so the operator notices.
    assert "<<<repair_proposal" in cleaned


# ── Render-then-parse equivalence for every valid action ───────────────────


def test_every_valid_action_round_trips():
    for action in sorted(VALID_ACTIONS):
        payload = {"k": "v"} if action != "done" else {}
        block = render_proposal_block(action, payload)
        _, proposals = extract_proposals(block)
        assert len(proposals) == 1, f"action {action} did not parse"
        assert proposals[0].action == action
        assert proposals[0].payload == payload


def test_render_unknown_action_raises():
    with pytest.raises(ValueError):
        render_proposal_block("delete_universe", {})


# ── Channel-format portability ─────────────────────────────────────────────
#
# The whole point of this module: a block built by Channel B's prompt
# template can be parsed by Channel A's apply path (and vice versa).
# Until PR #2375 merges to main, repair_chat lives on a sibling branch
# and isn't importable here. The format portability is enforced by
# the shape contract instead:
#   * Block opens with literal "<<<repair_proposal action=\"X\">>>"
#   * Block closes with literal "<<<end>>>"
#   * VALID_ACTIONS frozenset matches the union both channels use


def test_block_format_is_portable():
    """The literal tag shape is stable — neither channel may diverge.

    Both Channel A's repair_chat.extract_proposals (PR #2375) and this
    module's extract_proposals use the same regex anchors; this test
    pins the literal form so a future refactor doesn't drift one without
    the other.
    """
    block = render_proposal_block("done", {})
    assert block.startswith('<<<repair_proposal action="done">>>')
    assert block.endswith("<<<end>>>")


def test_valid_actions_set_matches_channel_a():
    """VALID_ACTIONS must equal repair_chat's set (once merged).

    Channel A's set (per PR #2375) is the same five action names; this
    test pins our side so we don't add a sixth without updating the
    apply switch in repair_chat.apply_proposal.
    """
    assert VALID_ACTIONS == frozenset({
        "propose_field_edit",
        "propose_file_edit",
        "propose_test_exemption",
        "mark_resolved",
        "done",
    })

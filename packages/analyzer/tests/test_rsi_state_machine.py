"""tests/test_rsi_state_machine.py — Proposal state machine transitions."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.state_machine import (  # noqa: E402
    IllegalTransitionError,
    allowed_transitions,
    is_legal_transition,
    transition,
)
from testing.harness import make_investigation_proposal  # noqa: E402


def test_draft_to_pending_legal():
    p = make_investigation_proposal()
    assert p.status == "draft"
    transition(p, "pending", actor="arbiter")
    assert p.status == "pending"
    assert len(p.history) == 1
    assert p.history[0].from_status == "draft"
    assert p.history[0].to_status == "pending"


def test_pending_to_approved_auto_legal():
    p = make_investigation_proposal()
    transition(p, "pending", actor="arbiter")
    transition(p, "approved_auto", actor="arbiter")
    assert p.status == "approved_auto"


def test_pending_to_approved_human_legal():
    p = make_investigation_proposal()
    transition(p, "pending", actor="arbiter")
    transition(p, "approved_human", actor="user")
    assert p.status == "approved_human"


def test_pending_to_dismissed_and_rejected_legal():
    for target in ("dismissed", "rejected", "snoozed"):
        p = make_investigation_proposal()
        transition(p, "pending", actor="arbiter")
        transition(p, target, actor="user")
        assert p.status == target


def test_applied_to_terminal_states_legal():
    for target in (
        "succeeded",
        "failed_reverted",
        "failed_flagged",
        "failed_revert_failed",
        "dismissed",
    ):
        p = make_investigation_proposal()
        transition(p, "pending", actor="arbiter")
        transition(p, "approved_auto", actor="arbiter")
        transition(p, "applied", actor="arbiter")
        transition(p, target, actor="verify_daemon")
        assert p.status == target


def test_snoozed_back_to_pending_legal():
    p = make_investigation_proposal()
    transition(p, "pending", actor="arbiter")
    transition(p, "snoozed", actor="user")
    transition(p, "pending", actor="snooze_wake")
    assert p.status == "pending"


def test_illegal_transition_from_draft_to_applied():
    p = make_investigation_proposal()
    with pytest.raises(IllegalTransitionError):
        transition(p, "applied", actor="arbiter")


def test_illegal_transition_from_terminal():
    p = make_investigation_proposal()
    transition(p, "pending", actor="arbiter")
    transition(p, "rejected", actor="user")
    with pytest.raises(IllegalTransitionError):
        transition(p, "pending", actor="user")


def test_allowed_transitions_returns_empty_set_for_terminal():
    assert allowed_transitions("succeeded") == frozenset()
    assert allowed_transitions("rejected") == frozenset()
    assert allowed_transitions("superseded") == frozenset()


def test_is_legal_transition():
    assert is_legal_transition("draft", "pending")
    assert not is_legal_transition("draft", "applied")
    assert is_legal_transition("pending", "snoozed")
    assert not is_legal_transition("snoozed", "applied")


def test_history_records_actor_and_reason():
    p = make_investigation_proposal()
    transition(p, "pending", actor="arbiter", reason="smoke test")
    assert p.history[0].actor == "arbiter"
    assert p.history[0].reason == "smoke test"
    assert p.history[0].at.endswith("+00:00")

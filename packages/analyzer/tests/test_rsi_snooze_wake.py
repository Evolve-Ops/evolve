"""tests/test_rsi_snooze_wake.py — snooze-wake daemon."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.snooze_wake import wake_expired  # noqa: E402
from arbiter.state_machine import transition  # noqa: E402
from testing.harness import make_investigation_proposal  # noqa: E402


def _put_in_snooze(p, wake_at):
    transition(p, "pending", actor="arbiter")
    transition(p, "snoozed", actor="user")
    p.snoozed_until = wake_at.isoformat(timespec="seconds")
    return p


def test_wakes_proposal_whose_time_has_passed():
    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    past = now - timedelta(days=1)
    p = _put_in_snooze(make_investigation_proposal(), past)

    result = wake_expired([p], now=now)
    assert p.id in result.woken
    assert p.status == "pending"
    assert p.snoozed_until is None


def test_leaves_proposals_whose_time_has_not_passed():
    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    future = now + timedelta(days=3)
    p = _put_in_snooze(make_investigation_proposal(), future)

    result = wake_expired([p], now=now)
    assert p.id in result.still_snoozed
    assert p.status == "snoozed"


def test_skips_proposal_with_malformed_wake_time():
    p = make_investigation_proposal()
    transition(p, "pending", actor="arbiter")
    transition(p, "snoozed", actor="user")
    p.snoozed_until = "not-a-date"

    result = wake_expired([p])
    assert any(pid == p.id for pid, _ in result.skipped)
    assert p.status == "snoozed"


def test_skips_proposal_not_actually_snoozed():
    p = make_investigation_proposal()
    transition(p, "pending", actor="arbiter")  # status == "pending", not "snoozed"
    p.snoozed_until = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    result = wake_expired([p])
    assert any(pid == p.id for pid, _ in result.skipped)


def test_records_transition_history():
    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    past = now - timedelta(days=1)
    p = _put_in_snooze(make_investigation_proposal(), past)

    wake_expired([p], now=now, actor="snooze_wake")
    assert p.history[-1].from_status == "snoozed"
    assert p.history[-1].to_status == "pending"
    assert p.history[-1].actor == "snooze_wake"

"""Tests for board_stack.py — D-ST1..9's pure composition and the two small
writers (the seen counter, the Approve/Decline decision).

WHAT THESE PIN:
  * stack_order's exact D-ST2 group order: briefing -> bot returns ->
    today (by when, then add time) -> inbox (by add time) -> later-due;
    done/dropped never, and a not-yet-due later card never.
  * The third-pass warning fires exactly at the threshold, resets across a
    day boundary, and undo removes the increment without a second event.
  * record_decision maps approve/decline onto the store's EXISTING
    delegation states (no new event type) and refuses a card not owned by
    the bot, exactly as ``set_delegation_progress`` already does.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import board_stack as stack  # noqa: E402
from evolve_admin import board_store as bs  # noqa: E402

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def shared(tmp_path: Path) -> Path:
    d = tmp_path / "evolve"
    d.mkdir()
    return d


def _card(shared, bot_id="bot-a", **kw):
    board = bs.load_board(shared, bot_id)
    card = bs.add_card(board, **kw)
    bs.save_board(shared, bot_id, board)
    return card


# ── stack_order ──────────────────────────────────────────────────────────

def test_stack_order_group_sequence_and_never_settled():
    returned = {
        "id": "r1", "lane": "today", "owner": "bot", "created_at": "2026-09-14T08:00:00Z",
        "delegation": {"state": "returned_for_review", "updated_at": "2026-09-14T09:00:00Z"},
    }
    today_timed = {
        "id": "t1", "lane": "today", "owner": "me", "created_at": "2026-09-14T07:00:00Z",
        "enrichment": {"when": {"value": {"start": "2026-09-14T16:00:00Z"}, "source": "calendar"}},
    }
    today_untimed = {
        "id": "t2", "lane": "today", "owner": "me", "created_at": "2026-09-14T06:00:00Z",
    }
    inbox = {"id": "i1", "lane": "inbox", "owner": "me", "created_at": "2026-09-14T05:00:00Z"}
    later_due = {
        "id": "l1", "lane": "later", "owner": "me", "created_at": "2026-09-14T04:00:00Z",
        "snooze_until": "2026-09-14T00:00:00Z",
    }
    later_not_due = {
        "id": "l2", "lane": "later", "owner": "me", "created_at": "2026-09-14T03:00:00Z",
        "snooze_until": "2026-09-20T00:00:00Z",
    }
    done = {"id": "d1", "lane": "done", "owner": "me", "created_at": "2026-09-14T02:00:00Z"}
    dropped = {"id": "x1", "lane": "dropped", "owner": "me", "created_at": "2026-09-14T01:00:00Z"}

    items = stack.stack_order(
        [today_untimed, dropped, later_not_due, inbox, done, today_timed, later_due, returned],
        None, now=NOW,
    )
    ids = [i["card"]["id"] for i in items]
    # returns, then today sorted by WHEN (timed before untimed), then
    # inbox, then later-due. later_not_due, done, dropped: absent.
    assert ids == ["r1", "t1", "t2", "i1", "l1"]


def test_stack_order_briefing_is_card_zero_unless_dismissed():
    briefing = {"title": "Morning", "dismissed": False}
    items = stack.stack_order([], briefing, now=NOW)
    assert items == [{"kind": "briefing", "briefing": briefing}]

    dismissed = {"title": "Morning", "dismissed": True}
    assert stack.stack_order([], dismissed, now=NOW) == []


def test_stack_order_is_pure_and_deterministic():
    cards = [{"id": "a", "lane": "inbox", "owner": "me", "created_at": "2026-09-14T00:00:00Z"}]
    first = stack.stack_order(cards, None, now=NOW)
    second = stack.stack_order(cards, None, now=NOW)
    assert first == second
    assert cards[0] == {"id": "a", "lane": "inbox", "owner": "me",
                         "created_at": "2026-09-14T00:00:00Z"}  # unmutated


def test_stack_order_unparseable_snooze_fails_toward_visible():
    card = {"id": "l1", "lane": "later", "owner": "me", "created_at": "x",
            "snooze_until": "not-a-date"}
    items = stack.stack_order([card], None, now=NOW)
    assert [i["card"]["id"] for i in items] == ["l1"]


# ── card_why ─────────────────────────────────────────────────────────────

def test_card_why_prefers_why_saved():
    card = {"source": "calendar",
            "enrichment": {"why_saved": {"value": "flight is at 4", "source": "bot"}}}
    assert stack.card_why(card) == "flight is at 4"


def test_card_why_falls_back_to_source():
    assert stack.card_why({"source": "calendar"}) == "from calendar"
    assert stack.card_why({"source": "manual"}) == "added by you"
    assert stack.card_why({"source": "widget"}) == "from widget"


# ── record_card_seen ─────────────────────────────────────────────────────

def test_seen_increments_and_warns_at_third_pass(shared):
    card = _card(shared, title="Renew license", cluster="admin")
    for expected_count, expected_warn in ((1, False), (2, False), (3, True), (4, True)):
        updated, warn = stack.record_card_seen(shared, "bot-a", card["id"], actor="user", now=NOW)
        assert updated["seen_today"] == {"date": "2026-09-14", "count": expected_count}
        assert warn is expected_warn

    day_path = bs.board_dir(shared, "bot-a") / "events" / "2026-09-14.jsonl"
    rows = [json.loads(line) for line in day_path.read_text().splitlines()]
    assert [r["event"] for r in rows] == ["seen", "seen", "seen", "seen"]


def test_seen_event_lands_on_the_injected_day_not_the_wall_clock(shared):
    """The card's state and the event row must agree on WHICH DAY they describe.

    `record_card_seen` is handed a clock and uses it for `seen_today`; the event
    log used `datetime.now()` regardless, so the card said one day and the row
    describing that write said another. It was invisible while the two agreed
    and turned CI red the moment the wall clock drifted past this file's NOW —
    which is a test passing for a reason that has nothing to do with the code.

    Pinned to a date that is deliberately neither NOW nor today, so this test
    cannot start passing (or failing) because of the date it is run on.
    """
    far = datetime(2019, 3, 4, 8, 0, 0, tzinfo=timezone.utc)
    card = _card(shared, title="Injected clock", cluster="admin")
    stack.record_card_seen(shared, "bot-a", card["id"], actor="user", now=far)

    events = bs.board_dir(shared, "bot-a") / "events"
    days = sorted(p.name for p in events.glob("*.jsonl"))
    assert days == ["2019-03-04.jsonl"], (
        "the event row landed on a different day than the card state: %s" % days)
    row = json.loads((events / "2019-03-04.jsonl").read_text().splitlines()[0])
    assert row["ts"].startswith("2019-03-04T08:00:00")


def test_seen_resets_across_a_day_boundary(shared):
    card = _card(shared, title="Water the plants", cluster="home")
    for _ in range(3):
        stack.record_card_seen(shared, "bot-a", card["id"], actor="user", now=NOW)
    tomorrow = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)
    updated, warn = stack.record_card_seen(shared, "bot-a", card["id"], actor="user", now=tomorrow)
    assert updated["seen_today"] == {"date": "2026-09-15", "count": 1}
    assert warn is False


def test_seen_undo_decrements_without_a_second_event(shared):
    card = _card(shared, title="Pay rent", cluster="admin")
    stack.record_card_seen(shared, "bot-a", card["id"], actor="user", now=NOW)
    stack.record_card_seen(shared, "bot-a", card["id"], actor="user", now=NOW)
    updated, warn = stack.record_card_seen(
        shared, "bot-a", card["id"], actor="user", undo=True, now=NOW)
    assert updated["seen_today"]["count"] == 1
    assert warn is False
    day_path = bs.board_dir(shared, "bot-a") / "events" / "2026-09-14.jsonl"
    rows = [json.loads(line) for line in day_path.read_text().splitlines()]
    assert [r["event"] for r in rows] == ["seen", "seen"]  # no "unseen" row


def test_seen_undo_never_goes_negative(shared):
    card = _card(shared, title="Call plumber", cluster="home")
    updated, _ = stack.record_card_seen(
        shared, "bot-a", card["id"], actor="user", undo=True, now=NOW)
    assert updated["seen_today"]["count"] == 0


def test_seen_unknown_card_raises_keyerror(shared):
    bs.save_board(shared, "bot-a", bs.load_board(shared, "bot-a"))
    with pytest.raises(KeyError):
        stack.record_card_seen(shared, "bot-a", "no-such-id", actor="user", now=NOW)


# ── record_decision ──────────────────────────────────────────────────────

def test_decision_approve_sets_done(shared):
    card = _card(shared, title="Draft the reply", cluster="work", owner="bot")
    bs.set_delegation_progress(shared, "bot-a", card["id"],
                                state="returned_for_review", actor="bot")
    updated = stack.record_decision(shared, "bot-a", card["id"],
                                    decision="approve", actor="user")
    assert updated["delegation"]["state"] == "done"
    assert "approved by user" in updated["delegation"]["progress_note"]


def test_decision_decline_sets_blocked_with_note(shared):
    card = _card(shared, title="Book the flight", cluster="travel", owner="bot")
    bs.set_delegation_progress(shared, "bot-a", card["id"],
                                state="returned_for_review", actor="bot")
    updated = stack.record_decision(shared, "bot-a", card["id"],
                                    decision="decline", note="wrong dates", actor="user")
    assert updated["delegation"]["state"] == "blocked"
    assert updated["delegation"]["progress_note"] == "declined by user — wrong dates"


def test_decision_invalid_value_raises(shared):
    card = _card(shared, title="Whatever", cluster="admin", owner="bot")
    with pytest.raises(ValueError):
        stack.record_decision(shared, "bot-a", card["id"], decision="maybe", actor="user")


def test_decision_refuses_a_card_not_owned_by_the_bot(shared):
    card = _card(shared, title="Mine", cluster="admin")  # owner defaults to "me"
    with pytest.raises(ValueError):
        stack.record_decision(shared, "bot-a", card["id"], decision="approve", actor="user")


def test_decision_refuses_a_bot_card_still_offered(shared):
    card = _card(shared, title="Draft the reply", cluster="work")
    bs.assign_card(shared, "bot-a", card["id"], "bot", actor="user")
    with pytest.raises(stack.CardNotReadyForDecision, match="offered"):
        stack.record_decision(shared, "bot-a", card["id"], decision="approve", actor="user")


def test_decision_refuses_a_bot_card_still_in_progress(shared):
    card = _card(shared, title="Draft the reply", cluster="work", owner="bot")
    bs.set_delegation_progress(shared, "bot-a", card["id"],
                                state="in_progress", actor="bot")
    with pytest.raises(stack.CardNotReadyForDecision, match="in_progress"):
        stack.record_decision(shared, "bot-a", card["id"], decision="approve", actor="user")


# ── today_stats / bots_plate ─────────────────────────────────────────────

def test_today_stats_counts_from_the_event_log(shared):
    a = _card(shared, title="A", cluster="admin")
    b = _card(shared, title="B", cluster="admin")
    c = _card(shared, title="C", cluster="admin")
    bs.move_card(shared, "bot-a", a["id"], "done", actor="user")
    bs.move_card(shared, "bot-a", b["id"], "dropped", actor="user", reason="never")
    bs.assign_card(shared, "bot-a", c["id"], "bot", actor="user")
    stats = stack.today_stats(shared, "bot-a")
    assert stats == {"done_today": 1, "dropped_today": 1, "handed_off_today": 1}


def test_today_stats_empty_log_is_all_zero(shared):
    assert stack.today_stats(shared, "no-events-yet") == {
        "done_today": 0, "dropped_today": 0, "handed_off_today": 0,
    }


def test_bots_plate_only_accepted_and_in_progress():
    cards = [
        {"owner": "bot", "delegation": {"state": "offered"}},
        {"owner": "bot", "delegation": {"state": "accepted"}},
        {"owner": "bot", "delegation": {"state": "in_progress"}},
        {"owner": "bot", "delegation": {"state": "returned_for_review"}},
        {"owner": "me", "delegation": {"state": "accepted"}},
    ]
    plate = stack.bots_plate(cards)
    assert [c["delegation"]["state"] for c in plate] == ["accepted", "in_progress"]

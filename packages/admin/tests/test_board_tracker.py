"""Tests for the Tracker fields (D-TM1/2/5/8): lists, outcome/owner/pace on
a card, the cadence table, and the BOARD.md mirror.

Design: internal/design-pa-tasks-and-follow-through-2026-09-18.md.

WHAT THESE PIN:
  * A card round-trips with and without every new field — old cards load
    unchanged, and ``list_id`` is the only thing migration ever stamps.
  * The two schema refusals the brief names: a goal two levels deep, and a
    ``next_touch`` with no ``touch_action``.
  * :func:`next_touch_for`'s table for all five cadence classes, the ``due``
    override (including "already past -> now"), and the back-off/cap.
  * The BOARD.md mirror's line shape and its atomic, always-somewhere write.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import board_stack as stack  # noqa: E402
from evolve_admin import board_store as bs  # noqa: E402

NOW = datetime(2026, 9, 21, 10, 0, 0, tzinfo=timezone.utc)


# ── schema round-trip ────────────────────────────────────────────────────

def test_card_roundtrip_without_new_fields(tmp_path: Path):
    """An ordinary old-style add_card call carries no Tracker field except
    the ``list_id`` every card now gets — additive, nothing else changes."""
    card = bs.create_card(tmp_path, "b", title="Book scan", cluster="health")
    assert card["list_id"] == bs.DEFAULT_LIST_ID
    for field in ("outcome", "assignee", "reporter", "severity", "area",
                  "due", "waiting_on", "cadence", "next_touch",
                  "touch_action", "touches", "belong_to", "human_id"):
        assert field not in card


def test_card_roundtrip_with_new_fields(tmp_path: Path):
    card = bs.create_card(
        tmp_path, "b", title="Fix the leak", cluster="home",
        outcome="leak repaired and confirmed dry", severity=2, area="plumbing",
        due="2026-09-30",
    )
    again = bs.load_board(tmp_path, "b")
    reloaded = bs.find_card(again, card["id"])
    assert reloaded["outcome"] == "leak repaired and confirmed dry"
    assert reloaded["severity"] == 2
    assert reloaded["area"] == "plumbing"
    assert reloaded["due"] == "2026-09-30"
    assert reloaded["list_id"] == bs.DEFAULT_LIST_ID


def test_legacy_board_migrates_personal_list_and_stamps_list_id(tmp_path: Path):
    """A board saved before this chip (no ``lists`` key, a card with no
    ``list_id``) gains both on load — idempotently, in memory."""
    raw = {"schema_version": 1, "bot_id": "b", "cards": [
        {"id": "c1" * 16, "title": "Old card", "cluster": "admin", "lane": "inbox"},
    ]}
    bs.board_path(tmp_path, "b").parent.mkdir(parents=True)
    import json
    bs.board_path(tmp_path, "b").write_text(json.dumps(raw))
    board = bs.load_board(tmp_path, "b")
    assert bs.find_list(board, bs.DEFAULT_LIST_ID) is not None
    assert board["cards"][0]["list_id"] == bs.DEFAULT_LIST_ID
    # Idempotent: loading again changes nothing further.
    assert bs.migrate_board(board) is False


# ── owner "other" / waiting_on ───────────────────────────────────────────

def test_owner_other_requires_waiting_on(tmp_path: Path):
    board = bs.load_board(tmp_path, "b")
    with pytest.raises(ValueError, match="waiting_on"):
        bs.add_card(board, title="X", cluster="admin", owner="other")


def test_owner_other_stores_waiting_on_and_defaults_since(tmp_path: Path):
    board = bs.load_board(tmp_path, "b")
    card = bs.add_card(board, title="X", cluster="admin", owner="other",
                       waiting_on={"who": "Bob", "source": "text"})
    assert card["waiting_on"]["who"] == "Bob"
    assert card["waiting_on"]["since"]  # stamped


def test_waiting_on_rejected_when_owner_is_not_other(tmp_path: Path):
    board = bs.load_board(tmp_path, "b")
    with pytest.raises(ValueError, match="only accepted when owner"):
        bs.add_card(board, title="X", cluster="admin", owner="me",
                    waiting_on={"who": "Bob", "source": "text"})


def test_assign_card_to_other_and_back(tmp_path: Path):
    card = bs.create_card(tmp_path, "b", title="X", cluster="admin")
    bs.assign_card(tmp_path, "b", card["id"], "other", actor="user",
                   waiting_on={"who": "Bob", "source": "text"})
    board = bs.load_board(tmp_path, "b")
    reloaded = bs.find_card(board, card["id"])
    assert reloaded["owner"] == "other"
    assert reloaded["waiting_on"]["who"] == "Bob"
    bs.assign_card(tmp_path, "b", card["id"], "me", actor="user")
    board = bs.load_board(tmp_path, "b")
    reloaded = bs.find_card(board, card["id"])
    assert reloaded["owner"] == "me"
    assert "waiting_on" not in reloaded


# ── lists (build item 0) ─────────────────────────────────────────────────

def test_create_list_project_requires_id_prefix(tmp_path: Path):
    with pytest.raises(ValueError, match="id_prefix"):
        bs.create_list(tmp_path, "b", name="Ops", shape="project", actor="t")


def test_create_list_assistant_forbids_id_prefix(tmp_path: Path):
    with pytest.raises(ValueError, match="id_prefix"):
        bs.create_list(tmp_path, "b", name="Mine", shape="assistant",
                       id_prefix="OP", actor="t")


def test_create_list_mints_project_ids_never_reused(tmp_path: Path):
    lst = bs.create_list(tmp_path, "b", name="Ops", shape="project",
                         id_prefix="OP", members=[{"id": "alice", "display": "Alice"}],
                         actor="t")
    board = bs.load_board(tmp_path, "b")
    c1 = bs.add_card(board, title="one", cluster="admin", list_id=lst["list_id"])
    c2 = bs.add_card(board, title="two", cluster="admin", list_id=lst["list_id"])
    assert c1["human_id"] == "OP-0001"
    assert c2["human_id"] == "OP-0002"
    bs.save_board(tmp_path, "b", board)
    board = bs.load_board(tmp_path, "b")
    c3 = bs.add_card(board, title="three", cluster="admin", list_id=lst["list_id"])
    assert c3["human_id"] == "OP-0003"


def test_assignee_requires_project_shape_list(tmp_path: Path):
    board = bs.load_board(tmp_path, "b")
    with pytest.raises(ValueError, match="project-shape"):
        bs.add_card(board, title="X", cluster="admin", assignee="user")


def test_assignee_must_be_a_list_member(tmp_path: Path):
    lst = bs.create_list(tmp_path, "b", name="Ops", shape="project",
                         id_prefix="OP", members=[{"id": "alice", "display": "Alice"}],
                         actor="t")
    board = bs.load_board(tmp_path, "b")
    with pytest.raises(ValueError, match="not a member"):
        bs.add_card(board, title="X", cluster="admin", list_id=lst["list_id"],
                    assignee="bob")


def test_unknown_list_id_refused(tmp_path: Path):
    board = bs.load_board(tmp_path, "b")
    with pytest.raises(ValueError, match="unknown list_id"):
        bs.add_card(board, title="X", cluster="admin", list_id="nope")


# ── severity / due / reporter bounds ─────────────────────────────────────

@pytest.mark.parametrize("severity", [0, 4, "bad", True])
def test_invalid_severity_rejected(tmp_path: Path, severity):
    board = bs.load_board(tmp_path, "b")
    with pytest.raises(ValueError):
        bs.add_card(board, title="X", cluster="admin", severity=severity)


def test_invalid_due_rejected(tmp_path: Path):
    board = bs.load_board(tmp_path, "b")
    with pytest.raises(ValueError, match="due"):
        bs.add_card(board, title="X", cluster="admin", due="not-a-date")


def test_reporter_free_text_bounded(tmp_path: Path):
    board = bs.load_board(tmp_path, "b")
    with pytest.raises(ValueError, match="reporter"):
        bs.add_card(board, title="X", cluster="admin", reporter="x" * 200)


# ── pace: set_pace / record_touch / the schema error ─────────────────────

def test_set_pace_schema_error_next_touch_without_action(tmp_path: Path):
    """Refusal case #1 named by the brief: a card with next_touch but no
    touch_action is a schema error at write."""
    card = bs.create_card(tmp_path, "b", title="X", cluster="admin")
    with pytest.raises(ValueError, match="touch_action"):
        bs.set_pace(tmp_path, "b", card["id"],
                   next_touch="2026-09-25T00:00:00Z", actor="user")


def test_set_pace_clears_when_everything_is_none(tmp_path: Path):
    card = bs.create_card(tmp_path, "b", title="X", cluster="admin")
    bs.set_pace(tmp_path, "b", card["id"], cadence="soon",
               touch_action="remind", actor="user")
    cleared = bs.set_pace(tmp_path, "b", card["id"], actor="user")
    assert "cadence" not in cleared
    assert "next_touch" not in cleared
    assert "touch_action" not in cleared


def test_set_pace_explicit_next_touch_used_verbatim(tmp_path: Path):
    card = bs.create_card(tmp_path, "b", title="X", cluster="admin")
    paced = bs.set_pace(tmp_path, "b", card["id"],
                        next_touch="2099-01-01T00:00:00Z",
                        touch_action="ask", actor="user")
    assert paced["next_touch"] == "2099-01-01T00:00:00Z"


def test_set_pace_computes_next_touch_from_cadence(tmp_path: Path):
    card = bs.create_card(tmp_path, "b", title="X", cluster="admin")
    paced = bs.set_pace(tmp_path, "b", card["id"], cadence="now",
                        touch_action="remind", actor="user")
    assert paced["next_touch"]
    assert paced["cadence"] == "now"


def test_record_touch_appends_and_recomputes_next_touch(tmp_path: Path):
    card = bs.create_card(tmp_path, "b", title="X", cluster="admin")
    bs.set_pace(tmp_path, "b", card["id"], cadence="soon",
               touch_action="remind", actor="user")
    touched = bs.record_touch(tmp_path, "b", card["id"], action="remind",
                              result="no_change", actor="bot")
    assert len(touched["touches"]) == 1
    assert touched["touches"][0]["action"] == "remind"
    assert touched["next_touch"]  # recomputed, still on a clock


def test_record_touch_invalid_action_rejected(tmp_path: Path):
    card = bs.create_card(tmp_path, "b", title="X", cluster="admin")
    with pytest.raises(ValueError, match="touch action"):
        bs.record_touch(tmp_path, "b", card["id"], action="bogus", actor="bot")


def test_record_touch_with_no_cadence_clears_touch_action(tmp_path: Path):
    """A touch recorded on a card with no cadence has nothing for
    :func:`next_touch_for` to compute -> next_touch/touch_action both
    clear rather than left stale (which would trip the pace invariant)."""
    card = bs.create_card(tmp_path, "b", title="X", cluster="admin")
    touched = bs.record_touch(tmp_path, "b", card["id"], action="ask", actor="user")
    assert "next_touch" not in touched
    assert "touch_action" not in touched


# ── goals (D-TM8), including the second named refusal case ──────────────

def test_set_goal_links_and_clears(tmp_path: Path):
    card = bs.create_card(tmp_path, "b", title="Step", cluster="travel")
    goal = bs.create_card(tmp_path, "b", title="Trip", cluster="travel")
    linked = bs.set_goal(tmp_path, "b", card["id"], goal["id"], actor="user")
    assert linked["belong_to"] == goal["id"]
    cleared = bs.set_goal(tmp_path, "b", card["id"], None, actor="user")
    assert "belong_to" not in cleared


def test_set_goal_refuses_two_levels(tmp_path: Path):
    """Refusal case #2 named by the brief: a card whose belong_to target
    itself has belong_to is refused."""
    a = bs.create_card(tmp_path, "b", title="A", cluster="travel")
    goal = bs.create_card(tmp_path, "b", title="Goal", cluster="travel")
    grandgoal = bs.create_card(tmp_path, "b", title="Grand goal", cluster="travel")
    bs.set_goal(tmp_path, "b", goal["id"], grandgoal["id"], actor="user")
    with pytest.raises(ValueError, match="one level"):
        bs.set_goal(tmp_path, "b", a["id"], goal["id"], actor="user")


def test_set_goal_refuses_self(tmp_path: Path):
    card = bs.create_card(tmp_path, "b", title="A", cluster="travel")
    with pytest.raises(ValueError, match="own goal"):
        bs.set_goal(tmp_path, "b", card["id"], card["id"], actor="user")


# ── next_touch_for: the cadence table, back-off, cap, due override ──────

@pytest.mark.parametrize("cadence,expect_hours_at_least", [
    ("now", 0.4), ("soon", 47), ("later", 167), ("someday", 719),
])
def test_next_touch_for_first_touch_offsets(cadence, expect_hours_at_least):
    got = bs.next_touch_for({"cadence": cadence}, NOW)
    parsed = bs._parse_ts(got)
    hours = (parsed - NOW).total_seconds() / 3600
    assert hours >= expect_hours_at_least


def test_next_touch_for_today_targets_1800_local_utc_default():
    got = bs.next_touch_for({"cadence": "today"}, NOW)
    parsed = bs._parse_ts(got)
    assert parsed.hour == 18 and parsed.minute == 0
    assert parsed.date() == NOW.date()


def test_next_touch_for_today_rolls_to_tomorrow_after_1800():
    late = NOW.replace(hour=19)
    got = bs.next_touch_for({"cadence": "today"}, late)
    parsed = bs._parse_ts(got)
    assert parsed.date() == (late.date().replace(day=late.day + 1))
    assert parsed.hour == 18


def test_next_touch_for_no_cadence_no_due_is_none():
    assert bs.next_touch_for({}, NOW) is None
    assert bs.next_touch_for({"cadence": "bogus"}, NOW) is None


def test_next_touch_for_due_override():
    got = bs.next_touch_for({"due": "2026-09-25"}, NOW)
    parsed = bs._parse_ts(got)
    assert parsed.date().isoformat() == "2026-09-24"
    assert parsed.hour == 9


def test_next_touch_for_due_already_past_is_now():
    got = bs.next_touch_for({"due": "2026-09-01"}, NOW)
    assert got == bs._utcnow(NOW)


def test_next_touch_for_due_overrides_cadence():
    got = bs.next_touch_for({"cadence": "someday", "due": "2026-09-25"}, NOW)
    parsed = bs._parse_ts(got)
    assert parsed.date().isoformat() == "2026-09-24"


def test_next_touch_for_backs_off_on_trailing_no_movement():
    card = {"cadence": "soon"}
    first = bs._parse_ts(bs.next_touch_for(card, NOW))
    card["touches"] = [{"result": "no_change"}]
    second = bs._parse_ts(bs.next_touch_for(card, NOW))
    assert (second - NOW) > (first - NOW)


def test_next_touch_for_backoff_capped():
    card = {"cadence": "now",
           "touches": [{"result": "no_change"}] * 20}
    got = bs._parse_ts(bs.next_touch_for(card, NOW))
    assert (got - NOW) <= bs.CADENCE_DEFAULTS["now"]["cap"]


def test_next_touch_for_backoff_stops_at_first_movement():
    """The streak only counts a TRAILING run — a touch that moved something
    resets it, even if earlier touches didn't."""
    card = {"cadence": "soon", "touches": [
        {"result": "no_change"}, {"result": "no_change"}, {"result": "booked"},
    ]}
    got = bs._parse_ts(bs.next_touch_for(card, NOW))
    plain = bs._parse_ts(bs.next_touch_for({"cadence": "soon"}, NOW))
    assert got == plain  # no backoff applied — the last touch moved something


# ── BOARD.md mirror ───────────────────────────────────────────────────────

def test_render_card_mirror_line_shape():
    line = bs.render_card_mirror_line({
        "id": "abcdef1234567890", "title": "Chase deposit", "owner": "other",
        "waiting_on": {"who": "Alex"}, "next_touch": "2026-09-25T18:00:00Z",
        "touch_action": "remind", "due": "2026-09-30",
        "outcome": "deposit paid and confirmed",
    })
    assert line.startswith("- [")
    assert "Chase deposit" in line
    assert "waiting on Alex" in line
    assert "next 2026-09-25T18:00:00Z — remind" in line
    assert "due 2026-09-30" in line
    assert "outcome: deposit paid and confirmed" in line


def test_render_card_mirror_line_uses_human_id_when_present():
    line = bs.render_card_mirror_line({"id": "x" * 16, "human_id": "OP-0007",
                                       "title": "Agenda item", "owner": "me"})
    assert line.startswith("- [OP-0007]")


def test_render_board_mirror_groups_by_lane(tmp_path: Path):
    bs.create_card(tmp_path, "b", title="Inbox card", cluster="admin", lane="inbox")
    bs.create_card(tmp_path, "b", title="Today card", cluster="admin", lane="today")
    board = bs.load_board(tmp_path, "b")
    text = bs.render_board_mirror(board)
    assert "## Inbox" in text and "Inbox card" in text
    assert "## Today" in text and "Today card" in text
    assert "## Later" in text and "(none)" in text


def test_write_board_mirror_lands_somewhere_writable(tmp_path: Path):
    bs.create_card(tmp_path, "b", title="X", cluster="admin")
    path = bs.write_board_mirror(tmp_path, "b")
    assert path.exists()
    assert path.name == "BOARD.md"
    assert "X" in path.read_text()


def test_write_board_mirror_does_not_change_save_board_behavior(tmp_path: Path):
    """Guardrail check: an ordinary save/create path never touches the
    filesystem beyond board.json + the event log — the mirror is opt-in."""
    before = set(tmp_path.rglob("*"))
    bs.create_card(tmp_path, "b", title="X", cluster="admin")
    after = set(tmp_path.rglob("*"))
    new_paths = {p.name for p in (after - before)}
    assert "BOARD.md" not in new_paths


# ── board_stack.next_touch_line ──────────────────────────────────────────

def test_next_touch_line_buckets():
    assert stack.next_touch_line(
        {"next_touch": "2026-09-21T10:30:00Z", "touch_action": "remind"},
        now=NOW) == "next: remind in 30min"
    assert stack.next_touch_line(
        {"next_touch": "2026-09-19T10:00:00Z", "touch_action": "ask"},
        now=NOW) == "next: ask overdue 2d"


def test_next_touch_line_none_without_full_pace():
    assert stack.next_touch_line({}, now=NOW) is None
    assert stack.next_touch_line({"next_touch": "2026-09-21T10:30:00Z"}, now=NOW) is None
    assert stack.next_touch_line({"touch_action": "remind"}, now=NOW) is None

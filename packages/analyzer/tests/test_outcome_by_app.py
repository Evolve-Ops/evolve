"""tests/test_outcome_by_app.py — the outcome view rollup (design §3.2).

THE FIXTURE IS THE TEST (same discipline as test_apps_detail_fill.py): one
bot with two Tracker applications — an assistant-shape personal list (one
user) and a project-shape list with two members (two users) — each
exercising a different one of the eight counted measures through a REAL
event sequence, so every assertion below is checking a condition the rollup
actually had to compute, not a stub answer.

Board store events are written directly as JSON (not through board_store's
mutators) because those mutators stamp wall-clock ``now()`` with no clock
injection — the same reason ``usage_by_app``'s own tests build annotation
records by hand rather than calling a live writer. The shapes written here
are exactly what ``board_store.py``'s writers themselves produce (verified
against ``append_event``'s call sites).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from datetime import date

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

import outcome_by_app as oba  # noqa: E402
from outcome_by_app import (  # noqa: E402
    SCHEMA_VERSION,
    load_outcome_by_app,
    outcome_by_app_path,
    rollup_bot,
    run_outcome_by_app,
)

BOT = "bot-a"
TODAY = date(2026, 9, 22)


def _card(card_id: str, *, list_id: str, lane: str, created_at: str,
          **extra) -> dict:
    card = {
        "id": card_id, "title": card_id, "cluster": "admin", "lane": lane,
        "owner": extra.pop("owner", "me"), "source": "manual",
        "created_at": created_at, "list_id": list_id,
    }
    card.update(extra)
    return card


def _board() -> dict:
    return {
        "schema_version": 1, "bot_id": BOT, "cards": [
            _card("p1", list_id="personal", lane="today", created_at="2026-09-01T00:00:00Z"),
            _card("p2", list_id="personal", lane="done", created_at="2026-09-01T00:00:00Z",
                  owner="bot"),
            _card("p3", list_id="personal", lane="today", created_at="2026-09-01T00:00:00Z"),
            _card("p4", list_id="personal", lane="today", created_at="2026-09-05T00:00:00Z"),
            _card("p5", list_id="personal", lane="dropped", created_at="2026-09-05T00:00:00Z"),
            _card("o1", list_id="ops", lane="today", created_at="2026-09-15T00:00:00Z",
                  assignee="alice"),
            _card("o2", list_id="ops", lane="done", created_at="2026-09-10T00:00:00Z",
                  assignee="bob"),
            _card("o3", list_id="ops", lane="today", created_at="2026-09-01T00:00:00Z",
                  assignee="alice", owner="other",
                  waiting_on={"who": "the vendor", "source": "text",
                              "since": "2026-09-01T00:00:00Z"}),
            _card("o4", list_id="ops", lane="inbox", created_at="2026-09-01T00:00:00Z"),
        ],
        "lists": [
            {"list_id": "personal", "name": "Board", "shape": "assistant",
             "members": [{"id": "user", "display": "You"}]},
            {"list_id": "ops", "name": "Ops", "shape": "project", "id_prefix": "OP",
             "members": [{"id": "alice", "display": "Alice"},
                         {"id": "bob", "display": "Bob"}]},
        ],
    }


def _events() -> dict[str, list[dict]]:
    """day (YYYY-MM-DD) -> events written that day, across every card."""
    return {
        "2026-09-05": [
            {"event": "stocked", "card": "p4", "source": "proposal", "actor": "user"},
            {"event": "stocked", "card": "p5", "source": "proposal", "actor": "user"},
        ],
        "2026-09-06": [
            {"event": "triaged", "card": "p4", "from": "inbox", "to": "today", "actor": "user"},
            {"event": "dropped", "card": "p5", "from": "inbox", "to": "dropped", "actor": "user"},
        ],
        "2026-09-10": [
            {"event": "touch", "card": "p3", "action": "remind", "result": "", "actor": "bot"},
        ],
        "2026-09-12": [
            {"event": "touch", "card": "p3", "action": "remind", "result": "no_change", "actor": "bot"},
            {"event": "touch", "card": "p3", "action": "ask", "result": "still on", "actor": "user"},
        ],
        "2026-09-15": [
            {"event": "assigned", "card": "p2", "from": "me", "to": "bot", "actor": "user"},
        ],
        "2026-09-20": [
            {"event": "delegation", "card": "p2", "from": "accepted", "to": "done", "actor": "bot"},
        ],
        "2026-09-21": [
            {"event": "triaged", "card": "o1", "from": "inbox", "to": "today", "actor": "bot"},
        ],
        "2026-09-22": [
            {"event": "triaged", "card": "p1", "from": "inbox", "to": "today", "actor": "bot"},
            {"event": "triaged", "card": "o2", "from": "today", "to": "done", "actor": "bot"},
        ],
    }


def _write_fixture(shared_dir: Path) -> None:
    board_dir = shared_dir / "boards" / BOT
    board_dir.mkdir(parents=True)
    (board_dir / "board.json").write_text(json.dumps(_board()))
    events_dir = board_dir / "events"
    events_dir.mkdir()
    for day, rows in _events().items():
        lines = [json.dumps({"ts": f"{day}T12:00:00Z", **row}) for row in rows]
        (events_dir / f"{day}.jsonl").write_text("\n".join(lines) + "\n")


# ── The golden rollup ────────────────────────────────────────────────────────

def test_rollup_two_apps_two_users(tmp_path: Path):
    _write_fixture(tmp_path)
    payload = rollup_bot(tmp_path, BOT, today=TODAY)

    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["bot_id"] == BOT
    assert payload["as_of_date"] == "2026-09-22"
    assert set(payload["apps"]) == {"assistant", "project-manager"}

    assistant = payload["apps"]["assistant"]["windows"]
    assert assistant["d1"]["moved_by_bot"]["count"] == 1
    assert assistant["d1"]["moved_by_bot"]["card_ids"] == ["p1"]
    assert assistant["d1"]["resolved_by_bot"]["count"] == 0

    assert assistant["d7"]["moved_by_bot"]["count"] == 1
    assert assistant["d7"]["resolved_by_bot"]["count"] == 1
    assert assistant["d7"]["resolved_by_bot"]["card_ids"] == ["p2"]
    assert assistant["d7"]["jobs_finished_without_operator"]["count"] == 1
    assert assistant["d7"]["jobs_finished_without_operator"]["card_ids"] == ["p2"]
    # The reminder/proposal events (09-05..09-12) are outside the 7d window.
    assert assistant["d7"]["reminders_reasked"]["count"] == 0
    assert assistant["d7"]["proposals_accepted"]["count"] == 0

    assert assistant["d30"]["moved_by_bot"]["count"] == 1
    assert assistant["d30"]["resolved_by_bot"]["count"] == 1
    assert assistant["d30"]["jobs_finished_without_operator"]["count"] == 1
    assert assistant["d30"]["reminders_reasked"]["count"] == 1
    assert assistant["d30"]["reminders_reasked"]["card_ids"] == ["p3"]
    assert assistant["d30"]["reminders_acknowledged"]["count"] == 1
    assert assistant["d30"]["reminders_acknowledged"]["card_ids"] == ["p3"]
    assert assistant["d30"]["proposals_accepted"]["count"] == 1
    assert assistant["d30"]["proposals_accepted"]["card_ids"] == ["p4"]
    assert assistant["d30"]["proposals_dismissed"]["count"] == 1
    assert assistant["d30"]["proposals_dismissed"]["card_ids"] == ["p5"]
    # No "corrected in chat" event exists yet (D-TM4 unbuilt) — always 0.
    assert assistant["d30"]["proposals_edited"]["count"] == 0

    assistant_backlog = payload["apps"]["assistant"]["backlog"]
    assert assistant_backlog == {
        "open_cards": 3, "backlog_age_p50_days": 21.0,
        "backlog_age_max_days": 21.0, "blocked_over_3_days": 0,
    }

    pm = payload["apps"]["project-manager"]["windows"]
    assert pm["d1"]["resolved_by_bot"]["count"] == 1
    assert pm["d1"]["resolved_by_bot"]["card_ids"] == ["o2"]
    assert pm["d1"]["moved_by_bot"]["count"] == 0
    assert pm["d7"]["moved_by_bot"]["count"] == 1
    assert pm["d7"]["moved_by_bot"]["card_ids"] == ["o1"]
    assert pm["d7"]["resolved_by_bot"]["count"] == 1
    assert pm["d30"] == pm["d7"]  # every project-list event fell in the last 7 days

    # per bot x app x USER x day (build item 1) — the two members of the
    # project list get their own slice of the same accumulator.
    pm_users = payload["apps"]["project-manager"]["users"]
    assert set(pm_users) == {"alice", "bob", "unassigned"}
    assert pm_users["alice"]["d7"]["moved_by_bot"]["count"] == 1
    assert pm_users["alice"]["d7"]["moved_by_bot"]["card_ids"] == ["o1"]
    assert pm_users["alice"]["d7"]["resolved_by_bot"]["count"] == 0
    assert pm_users["bob"]["d7"]["resolved_by_bot"]["count"] == 1
    assert pm_users["bob"]["d7"]["resolved_by_bot"]["card_ids"] == ["o2"]
    assert pm_users["unassigned"]["d7"]["moved_by_bot"]["count"] == 0

    assistant_users = payload["apps"]["assistant"]["users"]
    assert set(assistant_users) == {"user"}
    assert assistant_users["user"]["d30"]["moved_by_bot"]["count"] == 1

    pm_backlog = payload["apps"]["project-manager"]["backlog"]
    assert pm_backlog == {
        "open_cards": 3, "backlog_age_p50_days": 21.0,
        "backlog_age_max_days": 21.0, "blocked_over_3_days": 1,
    }


def test_daily_series_carries_the_sparkline(tmp_path: Path):
    _write_fixture(tmp_path)
    payload = rollup_bot(tmp_path, BOT, today=TODAY)
    daily = payload["apps"]["assistant"]["daily"]
    assert daily["2026-09-22"]["moved_by_bot"] == 1
    assert daily["2026-09-12"]["reminders_reasked"] == 1
    assert daily["2026-09-12"]["reminders_acknowledged"] == 1
    assert daily["2026-09-10"]["reminders_reasked"] == 0
    assert len(daily) == 30  # MAX_WINDOW_DAYS


# ── Tri-state honesty: an untouched Tracker names no application ──────────

def test_empty_board_has_no_app_rows(tmp_path: Path):
    (tmp_path / "boards" / BOT).mkdir(parents=True)
    (tmp_path / "boards" / BOT / "board.json").write_text(json.dumps({
        "schema_version": 1, "bot_id": BOT, "cards": [],
        "lists": [{"list_id": "personal", "name": "Board", "shape": "assistant",
                   "members": [{"id": "user", "display": "You"}]}],
    }))
    payload = rollup_bot(tmp_path, BOT, today=TODAY)
    assert payload["apps"] == {}


def test_bot_with_no_board_at_all_has_no_app_rows(tmp_path: Path):
    payload = rollup_bot(tmp_path, "never-touched", today=TODAY)
    assert payload["apps"] == {}


# ── Output plumbing (mirrors usage_by_app's own coverage) ──────────────────

def test_write_and_load_round_trip(tmp_path: Path):
    _write_fixture(tmp_path)
    run_outcome_by_app(BOT, tmp_path, today=TODAY)
    path = outcome_by_app_path(tmp_path, BOT)
    assert path.exists()
    assert oct(path.stat().st_mode & 0o777) == oct(0o644)
    loaded = load_outcome_by_app(tmp_path, BOT)
    assert loaded["bot_id"] == BOT
    assert set(loaded["apps"]) == {"assistant", "project-manager"}


def test_load_outcome_by_app_missing_is_empty_dict(tmp_path: Path):
    assert load_outcome_by_app(tmp_path, "nobody") == {}

"""Tests for the Board store (board_store.py, slice 1).

WHAT THESE PIN:
  * **Fail-closed tokens.** No hash file, empty token, wrong token — all
    False, none raise. A board with no minted token accepts nobody.
  * **Only the hash touches disk, mode 0600.** A shared-dir read must never
    yield a usable credential.
  * **Bot-id validation is a path guard.** ``../`` shapes raise before any
    path join happens.
  * **The importer is idempotent by title and skips completed work** — the
    D-MB6 seed can run twice without doubling the board.
"""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import board_store as bs  # noqa: E402


def test_load_missing_board_is_empty(tmp_path: Path):
    board = bs.load_board(tmp_path, "personal-bot")
    assert board["cards"] == []
    assert board["bot_id"] == "personal-bot"


def test_save_load_roundtrip(tmp_path: Path):
    board = bs.load_board(tmp_path, "personal-bot")
    card = bs.add_card(board, title="Book scan", cluster="health", source="test")
    bs.save_board(tmp_path, "personal-bot", board)
    again = bs.load_board(tmp_path, "personal-bot")
    assert [c["id"] for c in again["cards"]] == [card["id"]]
    assert again["cards"][0]["lane"] == "inbox"


def test_add_card_rejects_bad_lane_cluster_and_blank_title(tmp_path: Path):
    board = bs.load_board(tmp_path, "b")
    with pytest.raises(ValueError):
        bs.add_card(board, title="x", cluster="health", lane="someday")
    with pytest.raises(ValueError):
        bs.add_card(board, title="x", cluster="Not A Slug!")
    with pytest.raises(ValueError):
        bs.add_card(board, title="   ", cluster="health")


def test_bot_id_traversal_rejected(tmp_path: Path):
    for bad in ("../evil", "a/b", "", "UPPER", ".hidden"):
        with pytest.raises(ValueError):
            bs.board_dir(tmp_path, bad)


def test_token_mint_verify_and_fail_closed(tmp_path: Path):
    # Nothing minted → nobody gets in, including the empty token.
    assert bs.verify_token(tmp_path, "bot-a", "anything") is False
    assert bs.verify_token(tmp_path, "bot-a", None) is False
    token = bs.mint_token(tmp_path, "bot-a")
    assert bs.verify_token(tmp_path, "bot-a", token) is True
    assert bs.verify_token(tmp_path, "bot-a", token + "x") is False
    # Another bot's board does not accept it.
    assert bs.verify_token(tmp_path, "bot-b", token) is False
    # Rotation invalidates the old token.
    newer = bs.mint_token(tmp_path, "bot-a")
    assert bs.verify_token(tmp_path, "bot-a", token) is False
    assert bs.verify_token(tmp_path, "bot-a", newer) is True


def test_token_file_holds_hash_only_mode_0600(tmp_path: Path):
    token = bs.mint_token(tmp_path, "bot-a")
    p = tmp_path / "boards" / "bot-a" / "token.sha256"
    on_disk = p.read_text().strip()
    assert token not in on_disk and len(on_disk) == 64
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_append_event_writes_utc_dated_jsonl(tmp_path: Path):
    bs.append_event(tmp_path, "bot-a", {"event": "triaged", "card": "abc", "to": "bot"})
    files = list((tmp_path / "boards" / "bot-a" / "events").glob("*.jsonl"))
    assert len(files) == 1
    row = json.loads(files[0].read_text().splitlines()[0])
    assert row["event"] == "triaged" and row["ts"].endswith("Z")


_TASKS_MD = """
# Task List

## 🔴 URGENT / THIS WEEK

| # | Task | Context | Who |
|---|------|---------|-----|
| 1 | Crown replacement | Call the dentist | Me |
| 0 | ~~Old resolved thing~~ | done | ~~Me~~ |

## 🔧 TECHNICAL TASKS

| # | Task | Context | Owner |
|---|------|---------|-------|
| T1 | Backup system audit | verify cron | Bot |

## ✅ COMPLETED

| # | Task | Completed |
|---|------|-----------|
| C1 | Set up tracking | Mar 19 |
"""


def test_import_parses_skips_done_and_struck_rows():
    rows = bs.import_tasks_md(_TASKS_MD)
    titles = [r["title"] for r in rows]
    assert "Crown replacement" in titles
    assert "Backup system audit" in titles
    assert all("Old resolved" not in t and "Set up tracking" not in t for t in titles)
    by_title = {r["title"]: r for r in rows}
    assert by_title["Crown replacement"]["cluster"] == "health"
    assert by_title["Backup system audit"]["cluster"] == "work"


def test_import_into_board_is_idempotent(tmp_path: Path):
    assert bs.import_tasks_into_board(tmp_path, "bot-a", _TASKS_MD) == 2
    assert bs.import_tasks_into_board(tmp_path, "bot-a", _TASKS_MD) == 0
    board = bs.load_board(tmp_path, "bot-a")
    assert len(board["cards"]) == 2
    assert all(c["source"] == "import" and c["lane"] == "inbox" for c in board["cards"])


def test_move_card_changes_only_when_and_logs_it(tmp_path: Path):
    """A move is a WHEN change and nothing else (D-BI7). It must not touch
    the owner, and it must not mint a delegation — handing a card over is
    ``assign_card``, which is the one place the offer is made."""
    board = bs.load_board(tmp_path, "b1")
    card = bs.add_card(board, title="Schedule scan", cluster="health")
    bs.save_board(tmp_path, "b1", board)
    moved = bs.move_card(tmp_path, "b1", card["id"], "today", actor="user")
    assert moved["lane"] == "today"
    assert moved["owner"] == "me"
    assert "delegation" not in moved
    rows = [json.loads(l) for f in (tmp_path / "boards" / "b1" / "events").glob("*.jsonl")
            for l in f.read_text().splitlines()]
    triaged = [r for r in rows if r["event"] == "triaged"]
    assert triaged and triaged[-1]["to"] == "today" and triaged[-1]["actor"] == "user"


def test_move_card_bad_lane_and_unknown_card(tmp_path: Path):
    board = bs.load_board(tmp_path, "b1")
    card = bs.add_card(board, title="X", cluster="admin")
    bs.save_board(tmp_path, "b1", board)
    with pytest.raises(ValueError):
        bs.move_card(tmp_path, "b1", card["id"], "someday", actor="user")
    with pytest.raises(KeyError):
        bs.move_card(tmp_path, "b1", "nope", "today", actor="user")


def test_split_card_forks_and_replaces_original(tmp_path: Path):
    board = bs.load_board(tmp_path, "b1")
    card = bs.add_card(board, title="Genomics", cluster="hobbies", lane="inbox")
    bs.save_board(tmp_path, "b1", board)
    kid_user, kid_bot = bs.split_card(
        tmp_path, "b1", card["id"],
        user_part="Download raw data", bot_part="Run the analysis", actor="user")
    after = bs.load_board(tmp_path, "b1")
    ids = {c["id"] for c in after["cards"]}
    assert card["id"] not in ids  # one card never sits in two lanes
    assert kid_user["lane"] == "today" and kid_user["parent_id"] == card["id"]
    # Two cards for two parts, differing by WHO, not by lane (D-BI7).
    assert kid_bot["lane"] == "today" and kid_bot["owner"] == "bot"
    assert kid_bot["delegation"]["state"] == "offered"


def test_split_requires_both_parts(tmp_path: Path):
    board = bs.load_board(tmp_path, "b1")
    card = bs.add_card(board, title="X", cluster="admin")
    bs.save_board(tmp_path, "b1", board)
    with pytest.raises(ValueError):
        bs.split_card(tmp_path, "b1", card["id"], user_part="", bot_part="y", actor="user")


# ── revoke + store bounds (F8 review: F-8, and the D-MB2 revoke surface) ────

def test_revoke_token_makes_verification_fail_closed(tmp_path: Path):
    token = bs.mint_token(tmp_path, "b1")
    assert bs.verify_token(tmp_path, "b1", token) is True
    assert bs.revoke_token(tmp_path, "b1") is True
    assert bs.verify_token(tmp_path, "b1", token) is False
    # Idempotent: revoking an already-revoked board is a no-op, not an error.
    assert bs.revoke_token(tmp_path, "b1") is False


def test_mint_after_revoke_issues_an_unrelated_credential(tmp_path: Path):
    first = bs.mint_token(tmp_path, "b1")
    bs.revoke_token(tmp_path, "b1")
    second = bs.mint_token(tmp_path, "b1")
    assert first != second
    assert bs.verify_token(tmp_path, "b1", first) is False
    assert bs.verify_token(tmp_path, "b1", second) is True


def test_card_text_is_bounded(tmp_path: Path):
    board = bs.load_board(tmp_path, "b1")
    with pytest.raises(ValueError, match="title is too long"):
        bs.add_card(board, title="x" * (bs.MAX_TITLE_CHARS + 1), cluster="admin")
    with pytest.raises(ValueError, match="note is too long"):
        bs.add_card(board, title="ok", note="y" * (bs.MAX_NOTE_CHARS + 1),
                    cluster="admin")
    # The bound is generous enough for a real task line.
    assert bs.add_card(board, title="x" * bs.MAX_TITLE_CHARS, cluster="admin")


def test_board_card_count_is_bounded(tmp_path: Path):
    board = {"schema_version": 1, "bot_id": "b1", "updated_at": "",
             "cards": [{"id": f"{i:032x}"} for i in range(bs.MAX_CARDS)]}
    with pytest.raises(ValueError, match="board is full"):
        bs.add_card(board, title="one too many", cluster="admin")


# ── D-BI7: lanes are WHEN, owner is WHO ───────────────────────────────────────


def test_a_legacy_bot_lane_card_migrates_on_load(tmp_path: Path):
    """The one-shot conversion, applied on every load and idempotent: a card
    parked in the retired ``bot`` lane becomes ``owner: bot`` in ``today``,
    keeping the offer that lane WAS. Written by hand here because no writer
    can produce a ``lane: bot`` card any more."""
    d = tmp_path / "boards" / "b1"
    d.mkdir(parents=True)
    (d / "board.json").write_text(json.dumps({
        "schema_version": 1, "bot_id": "b1", "updated_at": "2026-09-01T00:00:00Z",
        "cards": [
            {"id": "a" * 32, "title": "Find slots", "cluster": "health",
             "lane": "bot", "source": "manual", "created_at": "2026-09-01T00:00:00Z"},
            {"id": "b" * 32, "title": "Pick a date", "cluster": "health",
             "lane": "today", "source": "manual", "created_at": "2026-09-01T00:00:00Z"},
        ],
    }))
    board = bs.load_board(tmp_path, "b1")
    handed_over, mine = board["cards"]
    assert handed_over["lane"] == "today" and handed_over["owner"] == "bot"
    assert handed_over["delegation"]["state"] == "offered"
    assert mine["lane"] == "today" and mine["owner"] == "me"
    # Idempotent, and it does not re-offer a card whose delegation moved on.
    handed_over["delegation"] = {"state": "accepted", "updated_at": "x"}
    assert bs.migrate_board(board) is False
    assert board["cards"][0]["delegation"]["state"] == "accepted"


def test_the_migration_persists_on_the_next_write_not_on_read(tmp_path: Path):
    """``load_board`` migrates in memory only — a read path that writes is a
    read path that can fail on a full or read-only disk, and the phone reads
    every 30s. The next write is what lands the new shape."""
    d = tmp_path / "boards" / "b1"
    d.mkdir(parents=True)
    raw = {"schema_version": 1, "bot_id": "b1", "updated_at": "2026-09-01T00:00:00Z",
           "cards": [{"id": "a" * 32, "title": "Find slots", "cluster": "health",
                      "lane": "bot", "source": "manual",
                      "created_at": "2026-09-01T00:00:00Z"}]}
    (d / "board.json").write_text(json.dumps(raw))
    bs.load_board(tmp_path, "b1")
    assert json.loads((d / "board.json").read_text())["cards"][0]["lane"] == "bot"

    bs.move_card(tmp_path, "b1", "a" * 32, "later", actor="user")
    on_disk = json.loads((d / "board.json").read_text())["cards"][0]
    assert on_disk["lane"] == "later" and on_disk["owner"] == "bot"


def test_assign_offers_then_hands_back(tmp_path: Path):
    board = bs.load_board(tmp_path, "b1")
    card = bs.add_card(board, title="Find slots", cluster="health")
    bs.save_board(tmp_path, "b1", board)

    offered = bs.assign_card(tmp_path, "b1", card["id"], "bot", actor="user")
    assert offered["owner"] == "bot"
    assert offered["delegation"]["state"] == "offered"
    # Re-assigning to the owner it already has is a no-op, so a double tap
    # cannot reset an accepted delegation back to "offered".
    bs.set_delegation_progress(tmp_path, "b1", card["id"], state="accepted",
                               actor="bot")
    again = bs.assign_card(tmp_path, "b1", card["id"], "bot", actor="user")
    assert again["delegation"]["state"] == "accepted"

    back = bs.assign_card(tmp_path, "b1", card["id"], "me", actor="user")
    assert back["owner"] == "me" and "delegation" not in back
    rows = [json.loads(line)
            for f in (tmp_path / "boards" / "b1" / "events").glob("*.jsonl")
            for line in f.read_text().splitlines()]
    assigned = [r for r in rows if r["event"] == "assigned"]
    assert [(r["from"], r["to"]) for r in assigned] == [("me", "bot"), ("bot", "me")]


def test_progress_keeps_the_last_note_and_cost_when_omitted(tmp_path: Path):
    board = bs.load_board(tmp_path, "b1")
    card = bs.add_card(board, title="Find slots", cluster="health", owner="bot")
    bs.save_board(tmp_path, "b1", board)
    bs.set_delegation_progress(tmp_path, "b1", card["id"], state="in_progress",
                               note="left a message", cost_to_date=0.4, actor="bot")
    later = bs.set_delegation_progress(tmp_path, "b1", card["id"],
                                       state="returned_for_review", actor="bot")
    assert later["delegation"]["progress_note"] == "left a message"
    assert later["delegation"]["cost_to_date"] == 0.4


def test_add_card_rejects_a_bad_owner_and_a_negative_cost(tmp_path: Path):
    board = bs.load_board(tmp_path, "b1")
    with pytest.raises(ValueError, match="invalid owner"):
        bs.add_card(board, title="x", cluster="admin", owner="someone-else")
    card = bs.add_card(board, title="x", cluster="admin", owner="bot")
    bs.save_board(tmp_path, "b1", board)
    with pytest.raises(ValueError, match="negative"):
        bs.set_delegation_progress(tmp_path, "b1", card["id"], state="accepted",
                                   cost_to_date=-1, actor="bot")


# ── D-BI7 migration: the retired Bot lane ─────────────────────────────────

def _write_raw(tmp_path: Path, bot: str, cards: list) -> None:
    """Put a board on disk WITHOUT going through the writers — the only way
    to fixture a shape (``lane: bot``) the current writers refuse to mint."""
    d = tmp_path / "boards" / bot
    d.mkdir(parents=True, exist_ok=True)
    (d / "board.json").write_text(json.dumps(
        {"schema_version": 1, "bot_id": bot, "cards": cards}))


def test_legacy_bot_lane_migrates_to_owner_and_today(tmp_path: Path):
    _write_raw(tmp_path, "b", [
        {"id": "a" * 32, "title": "Handed over", "cluster": "work",
         "lane": "bot", "created_at": "2026-08-01T00:00:00Z"},
        {"id": "b" * 32, "title": "Mine", "cluster": "home",
         "lane": "today", "created_at": "2026-08-01T00:00:00Z"},
        # A hand-over the bot had already ACCEPTED keeps that state — the
        # migration must not reset live delegation back to an offer.
        {"id": "c" * 32, "title": "In flight", "cluster": "work",
         "lane": "bot", "created_at": "2026-08-01T00:00:00Z",
         "delegation": {"state": "in_progress", "updated_at": "2026-08-02T00:00:00Z"}},
    ])
    board = bs.load_board(tmp_path, "b")
    by_id = {c["id"]: c for c in board["cards"]}
    handed = by_id["a" * 32]
    assert handed["lane"] == "today" and handed["owner"] == "bot"
    assert handed["delegation"]["state"] == "offered"
    assert by_id["b" * 32]["owner"] == "me"
    assert by_id["c" * 32]["delegation"]["state"] == "in_progress"
    assert by_id["c" * 32]["owner"] == "bot"
    # No lane named 'bot' survives anywhere.
    assert not any(c["lane"] == bs.LEGACY_BOT_LANE for c in board["cards"])


def test_migration_is_idempotent_across_a_save_and_a_second_load(tmp_path: Path):
    _write_raw(tmp_path, "b", [
        {"id": "a" * 32, "title": "Handed over", "cluster": "work",
         "lane": "bot", "created_at": "2026-08-01T00:00:00Z"},
    ])
    first = bs.load_board(tmp_path, "b")
    assert bs.migrate_board(first) is False, "a migrated board re-migrates to a no-op"
    bs.save_board(tmp_path, "b", first)
    second = bs.load_board(tmp_path, "b")
    assert second["cards"] == first["cards"]
    assert bs.migrate_board(second) is False


# ── move: 'bot' is not a lane, and a drop may carry a reason ──────────────

def _one_card(tmp_path: Path, **kw) -> str:
    card = bs.create_card(tmp_path, "b", title=kw.pop("title", "A task"),
                          cluster=kw.pop("cluster", "home"), **kw)
    return card["id"]


def _events(tmp_path: Path, bot: str = "b") -> list[dict]:
    rows: list[dict] = []
    for p in sorted((tmp_path / "boards" / bot / "events").glob("*.jsonl")):
        rows += [json.loads(line) for line in p.read_text().splitlines() if line]
    return rows


def test_move_refuses_the_retired_bot_lane_and_names_assign(tmp_path: Path):
    card_id = _one_card(tmp_path)
    with pytest.raises(ValueError) as exc:
        bs.move_card(tmp_path, "b", card_id, "bot", actor="user")
    message = str(exc.value)
    assert "assign" in message and "owner" in message
    # …and the refusal happened before anything was written.
    assert bs.find_card(bs.load_board(tmp_path, "b"), card_id)["lane"] == "inbox"


def test_drop_reason_is_validated_and_lands_on_the_event(tmp_path: Path):
    card_id = _one_card(tmp_path, cluster="health", source="calendar")
    bs.move_card(tmp_path, "b", card_id, "dropped", actor="user",
                 reason="already handled")
    drops = [e for e in _events(tmp_path) if e["event"] == "dropped"]
    assert len(drops) == 1
    row = drops[0]
    # The learning loop's negative signal, whole: what, why, and in what
    # context — no second log to join against.
    assert row["reason"] == "already handled"
    assert row["cluster"] == "health"
    assert row["source"] == "calendar"
    assert row["owner"] == "me"
    assert row["actor"] == "user"
    assert row["from"] == "inbox" and row["to"] == "dropped"
    assert bs.find_card(bs.load_board(tmp_path, "b"), card_id)["drop_reason"] \
        == "already handled"


def test_a_drop_with_no_reason_is_still_a_drop(tmp_path: Path):
    card_id = _one_card(tmp_path)
    bs.move_card(tmp_path, "b", card_id, "dropped", actor="user")
    row = [e for e in _events(tmp_path) if e["event"] == "dropped"][0]
    assert row["reason"] is None
    assert "drop_reason" not in bs.find_card(bs.load_board(tmp_path, "b"), card_id)


def test_reason_is_refused_off_a_drop_and_off_the_fixed_set(tmp_path: Path):
    card_id = _one_card(tmp_path)
    with pytest.raises(ValueError, match="only accepted"):
        bs.move_card(tmp_path, "b", card_id, "today", actor="user",
                     reason="never")
    with pytest.raises(ValueError, match="invalid reason"):
        bs.move_card(tmp_path, "b", card_id, "dropped", actor="user",
                     reason="because I said so")


def test_moving_out_of_a_settled_lane_clears_its_stamp_and_reason(tmp_path: Path):
    card_id = _one_card(tmp_path)
    bs.move_card(tmp_path, "b", card_id, "dropped", actor="user", reason="never")
    bs.move_card(tmp_path, "b", card_id, "today", actor="user")
    card = bs.find_card(bs.load_board(tmp_path, "b"), card_id)
    # A card pulled back out is live again: it must not carry a clock that
    # would archive it the instant it is re-finished.
    assert "settled_at" not in card and "drop_reason" not in card


# ── retention: the record is forever, the tile is 30 days ─────────────────

def _age(tmp_path: Path, card_id: str, days: int) -> None:
    from datetime import datetime, timedelta, timezone
    board = bs.load_board(tmp_path, "b")
    when = datetime.now(timezone.utc) - timedelta(days=days)
    bs.find_card(board, card_id)["settled_at"] = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    bs.save_board(tmp_path, "b", board)


def test_settled_cards_leave_the_board_after_30_days_but_stay_on_disk(tmp_path: Path):
    fresh = _one_card(tmp_path, title="Dropped yesterday")
    old = _one_card(tmp_path, title="Dropped last quarter")
    live = _one_card(tmp_path, title="Still mine")
    bs.move_card(tmp_path, "b", fresh, "dropped", actor="user")
    bs.move_card(tmp_path, "b", old, "done", actor="user")
    _age(tmp_path, old, 31)
    board = bs.load_board(tmp_path, "b")
    shown = {c["id"] for c in bs.visible_cards(board)}
    assert shown == {fresh, live}
    everything = {c["id"] for c in bs.visible_cards(board, include_archived=True)}
    assert everything == {fresh, old, live}
    # Nothing was deleted — the record is the learning data.
    assert len(board["cards"]) == 3


def test_a_live_card_never_archives_however_old(tmp_path: Path):
    card_id = _one_card(tmp_path)
    _age(tmp_path, card_id, 400)   # a stamp it should not even have
    board = bs.load_board(tmp_path, "b")
    assert bs.is_archived(bs.find_card(board, card_id)) is False


def test_a_settled_card_with_an_unreadable_stamp_stays_visible(tmp_path: Path):
    _write_raw(tmp_path, "b", [
        {"id": "a" * 32, "title": "Done, somewhen", "cluster": "home",
         "lane": "done", "owner": "me", "settled_at": "not a timestamp"},
    ])
    board = bs.load_board(tmp_path, "b")
    # Fail toward a visible tile: a bad timestamp must not silently vanish a
    # card the user can still see is theirs.
    assert bs.visible_cards(board) == board["cards"]


def test_migration_stamps_settled_at_as_first_observed_not_creation(
        tmp_path: Path):
    """A card finished last week must not vanish because it was MADE in July.

    The backfill has no honest answer to "when was this settled?", so it
    records when it was first observed settled. Stamping ``created_at``
    instead would archive a recently-finished old card on the first load
    after deploy — the one moment the user is most likely to look for it,
    and with no UI in this chip to reach an archived card.
    """
    _write_raw(tmp_path, "b", [
        {"id": "a" * 32, "title": "Finished last week, made in January",
         "cluster": "home", "lane": "done",
         "created_at": "2026-01-01T00:00:00Z"},
    ])
    board = bs.load_board(tmp_path, "b")
    card = board["cards"][0]
    assert card["settled_at"] != "2026-01-01T00:00:00Z"
    assert bs.is_archived(card) is False
    assert card in bs.visible_cards(board)
    # And it still ages off normally from there — 30 days after the stamp,
    # not never.
    _age(tmp_path, "a" * 32, 31)
    assert bs.is_archived(bs.find_card(bs.load_board(tmp_path, "b"), "a" * 32))


def test_a_non_string_reason_or_source_id_is_refused_not_raised(tmp_path: Path):
    """A body is whatever JSON the caller sent.

    ``{"reason": ["never"]}`` used to reach ``.strip()`` and raise
    AttributeError — a 500 for the same class of mistake that gets a 400 one
    field over. Both fields refuse the same way now, and nothing is written.
    """
    card_id = _one_card(tmp_path)
    for bad in (["never"], 7, {"why": "never"}):
        with pytest.raises(ValueError, match="reason must be a string"):
            bs.move_card(tmp_path, "b", card_id, "dropped", actor="user",
                         reason=bad)
    assert bs.find_card(bs.load_board(tmp_path, "b"), card_id)["lane"] == "inbox"

    board = bs.load_board(tmp_path, "b")
    for bad in (42, ["evt-1"]):
        with pytest.raises(ValueError, match="source_id must be a string"):
            bs.add_card(board, title="x", cluster="home", source_id=bad)
    assert len(board["cards"]) == 1, "the refused add left no partial card"


# ── stocking dedup (D-BI2) ────────────────────────────────────────────────

def test_dedup_skips_a_candidate_the_user_already_settled(tmp_path: Path):
    dropped = _one_card(tmp_path, title="Standup", cluster="work",
                        source="calendar", source_id="evt-1")
    done = _one_card(tmp_path, title="Renew the passport", cluster="admin",
                     source="calendar", source_id="evt-2")
    bs.move_card(tmp_path, "b", dropped, "dropped", actor="user", reason="never")
    bs.move_card(tmp_path, "b", done, "done", actor="user")
    board = bs.load_board(tmp_path, "b")

    hit = bs.find_settled_duplicate(board, title="Standup", source_id="evt-1")
    assert hit is not None and hit["id"] == dropped
    assert bs.find_settled_duplicate(
        board, title="Renew the passport", source_id="evt-2") is not None
    # A fresh candidate is admitted.
    assert bs.find_settled_duplicate(
        board, title="Book the flights", source_id="evt-9") is None


def test_dedup_still_matches_an_archived_card(tmp_path: Path):
    old = _one_card(tmp_path, title="Weekly sync", cluster="work",
                    source="calendar", source_id="evt-7")
    bs.move_card(tmp_path, "b", old, "dropped", actor="user", reason="never")
    _age(tmp_path, old, 400)
    board = bs.load_board(tmp_path, "b")
    assert bs.is_archived(bs.find_card(board, old)) is True
    # The tile aged off the board; the DECISION did not age off. A card
    # dropped last year must not come back this morning.
    assert bs.find_settled_duplicate(
        board, title="Weekly sync", source_id="evt-7") is not None


def test_dedup_falls_back_to_a_normalised_title_and_day(tmp_path: Path):
    card = bs.create_card(
        tmp_path, "b", title="Dentist — 2pm", cluster="health",
        source="calendar",
        enrichment={"when": {"value": "2026-09-12T14:00:00Z",
                             "source": "calendar"}})
    bs.move_card(tmp_path, "b", card["id"], "dropped", actor="user")
    board = bs.load_board(tmp_path, "b")
    # Same task, a different rendering of the same day.
    assert bs.find_settled_duplicate(
        board, title="dentist  2pm", when="2026-09-12T09:00:00Z") is not None
    # Same title, a different day: a genuinely new occurrence.
    assert bs.find_settled_duplicate(
        board, title="Dentist — 2pm", when="2026-10-12T14:00:00Z") is None


def test_dedup_ignores_live_cards(tmp_path: Path):
    _one_card(tmp_path, title="Call the plumber", cluster="home",
              source_id="evt-3")
    board = bs.load_board(tmp_path, "b")
    # A task still ON the board is the caller's own duplicate problem;
    # skipping here would hide a real second occurrence of a recurring event.
    assert bs.find_settled_duplicate(
        board, title="Call the plumber", source_id="evt-3") is None


def test_dedup_refuses_to_match_on_an_empty_identity(tmp_path: Path):
    card = bs.create_card(tmp_path, "b", title="x", cluster="home")
    bs.move_card(tmp_path, "b", card["id"], "dropped", actor="user")
    board = bs.load_board(tmp_path, "b")
    # An identity of nothing would match every untitled card ever dropped.
    assert bs.find_settled_duplicate(board, title="   ") is None

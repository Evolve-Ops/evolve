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


def test_move_card_sets_lane_offer_and_event(tmp_path: Path):
    board = bs.load_board(tmp_path, "b1")
    card = bs.add_card(board, title="Schedule scan", cluster="health")
    bs.save_board(tmp_path, "b1", board)
    moved = bs.move_card(tmp_path, "b1", card["id"], "bot", actor="user")
    assert moved["lane"] == "bot"
    # Dragging to Bot is an offer, not a command.
    assert moved["delegation"]["state"] == "offered"
    rows = [json.loads(l) for f in (tmp_path / "boards" / "b1" / "events").glob("*.jsonl")
            for l in f.read_text().splitlines()]
    triaged = [r for r in rows if r["event"] == "triaged"]
    assert triaged and triaged[-1]["to"] == "bot" and triaged[-1]["actor"] == "user"


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
    assert kid_bot["lane"] == "bot" and kid_bot["delegation"]["state"] == "offered"


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

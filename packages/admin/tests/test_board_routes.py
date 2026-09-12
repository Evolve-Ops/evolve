"""Tests for the Board's read-only HTTP surface (routes_board.py, slice 1).

WHAT THESE PIN:
  * **Every route is token-gated, fail closed.** No token, wrong token,
    unminted board, hostile bot id — all 401, page and API alike, with no
    distinguishable "bot exists" oracle.
  * **Both presentations work** (Bearer header and ``?t=``) — the page uses
    the query form from a bookmark, the page's JS uses the header.
  * **ETag round-trips.** The page polls every 30s; an unchanged board must
    cost a 304, not a re-serialization to the phone.
  * **The page ships no data.** board.html is a static shell; the board
    itself only ever travels through the token-gated API.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import board_store as bs  # noqa: E402
from evolve_admin.web.routes_board import register_board_routes  # noqa: E402


@pytest.fixture()
def pod(tmp_path: Path):
    shared = tmp_path / "evolve"
    shared.mkdir()
    network = tmp_path / "network.json"
    network.write_text(json.dumps({"sharedDir": str(shared)}))
    board = bs.load_board(shared, "personal-bot")
    bs.add_card(board, title="Pick the trip weekend", cluster="travel", lane="today")
    bs.add_card(board, title="Schedule the scan", cluster="health", owner="bot")
    bs.save_board(shared, "personal-bot", board)
    token = bs.mint_token(shared, "personal-bot")
    app = Flask(__name__)
    register_board_routes(app, network)
    return {"client": app.test_client(), "shared": shared, "token": token}


def test_api_requires_token(pod):
    assert pod["client"].get("/api/board/personal-bot").status_code == 401


def test_api_rejects_wrong_token(pod):
    r = pod["client"].get("/api/board/personal-bot",
                          headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_unminted_and_unknown_bots_401_identically(pod):
    # An existing-but-unminted board and a nonexistent bot look the same.
    bs.save_board(pod["shared"], "other-bot", bs.load_board(pod["shared"], "other-bot"))
    for bot in ("other-bot", "no-such-bot"):
        r = pod["client"].get(f"/api/board/{bot}",
                              headers={"Authorization": "Bearer x"})
        assert r.status_code == 401


def test_hostile_bot_id_is_a_401_not_an_error(pod):
    r = pod["client"].get("/api/board/..%2f..%2fetc",
                          headers={"Authorization": f"Bearer {pod['token']}"})
    assert r.status_code in (401, 404)


def test_api_returns_board_with_bearer(pod):
    r = pod["client"].get("/api/board/personal-bot",
                          headers={"Authorization": f"Bearer {pod['token']}"})
    assert r.status_code == 200
    body = r.get_json()
    assert {c["title"] for c in body["cards"]} == {
        "Pick the trip weekend", "Schedule the scan"}
    assert r.headers.get("Cache-Control") == "no-store"


def test_api_accepts_query_token(pod):
    r = pod["client"].get(f"/api/board/personal-bot?t={pod['token']}")
    assert r.status_code == 200


def test_etag_304_roundtrip(pod):
    h = {"Authorization": f"Bearer {pod['token']}"}
    first = pod["client"].get("/api/board/personal-bot", headers=h)
    etag = first.headers["ETag"]
    again = pod["client"].get("/api/board/personal-bot",
                              headers={**h, "If-None-Match": etag})
    assert again.status_code == 304
    # A change invalidates it.
    board = bs.load_board(pod["shared"], "personal-bot")
    bs.add_card(board, title="New thing", cluster="admin")
    bs.save_board(pod["shared"], "personal-bot", board)
    changed = pod["client"].get("/api/board/personal-bot",
                                headers={**h, "If-None-Match": etag})
    assert changed.status_code == 200
    assert changed.headers["ETag"] != etag


def test_page_requires_token_and_serves_shell(pod):
    assert pod["client"].get("/board/personal-bot").status_code == 401
    # A ``?t=`` link no longer serves the page directly: it is upgraded to a
    # cookie and redirected to the clean URL (see test_board_auth.py). The
    # shell arrives at the end of that hop.
    r = pod["client"].get(f"/board/personal-bot?t={pod['token']}",
                          follow_redirects=True)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "text/html" in r.content_type
    # Static shell only — no card data baked into the page.
    assert "Pick the trip weekend" not in html


def _auth(pod):
    return {"Authorization": f"Bearer {pod['token']}"}


def test_writes_require_token(pod):
    c = pod["client"]
    assert c.post("/api/board/personal-bot/cards", json={"title": "x", "cluster": "admin"}).status_code == 401
    assert c.post("/api/board/personal-bot/cards/abc/move", json={"to_lane": "today"}).status_code == 401
    assert c.post("/api/board/personal-bot/cards/abc/split", json={"user_part": "a", "bot_part": "b"}).status_code == 401


def test_create_move_split_roundtrip(pod):
    c = pod["client"]
    r = c.post("/api/board/personal-bot/cards", headers=_auth(pod),
               json={"title": "Book dentist", "cluster": "health"})
    assert r.status_code == 201
    card = r.get_json()["card"]
    assert card["lane"] == "inbox"

    # WHEN and WHO are two different taps now (D-BI7): moving sets the lane
    # and nothing else; assigning is what offers the card to the bot.
    r = c.post(f"/api/board/personal-bot/cards/{card['id']}/move",
               headers=_auth(pod), json={"to_lane": "today"})
    assert r.status_code == 200
    assert r.get_json()["card"]["lane"] == "today"
    assert "delegation" not in r.get_json()["card"]

    r = c.post(f"/api/board/personal-bot/cards/{card['id']}/assign",
               headers=_auth(pod), json={"owner": "bot"})
    assert r.status_code == 200
    assert r.get_json()["card"]["delegation"]["state"] == "offered"

    r = c.post(f"/api/board/personal-bot/cards/{card['id']}/split",
               headers=_auth(pod),
               json={"user_part": "Pick the date", "bot_part": "Find open slots"})
    assert r.status_code == 200
    kids = r.get_json()["cards"]
    assert {k["lane"] for k in kids} == {"today"}
    assert {k["owner"] for k in kids} == {"me", "bot"}

    body = c.get("/api/board/personal-bot", headers=_auth(pod)).get_json()
    titles = {x["title"] for x in body["cards"]}
    assert "Pick the date" in titles and "Find open slots" in titles
    assert "Book dentist" not in titles


def test_write_validation_errors(pod):
    c = pod["client"]
    r = c.post("/api/board/personal-bot/cards", headers=_auth(pod),
               json={"title": "x", "cluster": "admin", "lane": "someday"})
    assert r.status_code == 400
    r = c.post("/api/board/personal-bot/cards/ghost/move", headers=_auth(pod),
               json={"to_lane": "today"})
    assert r.status_code == 404
    r = c.post("/api/board/personal-bot/cards", headers=_auth(pod), data="notjson",
               content_type="text/plain")
    assert r.status_code == 400


# ── D-BI2: the archive filter, and the drop reason on the wire ────────────

def _auth(pod):
    return {"Authorization": f"Bearer {pod['token']}"}


def _card_id(pod, title: str) -> str:
    board = bs.load_board(pod["shared"], "personal-bot")
    return next(c["id"] for c in board["cards"] if c["title"] == title)


def test_settled_cards_leave_the_default_read_and_return_with_include(pod):
    from datetime import datetime, timedelta, timezone
    card_id = _card_id(pod, "Pick the trip weekend")
    board = bs.load_board(pod["shared"], "personal-bot")
    card = bs.find_card(board, card_id)
    card["lane"] = "done"
    card["settled_at"] = (datetime.now(timezone.utc) - timedelta(days=45)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    bs.save_board(pod["shared"], "personal-bot", board)

    default = pod["client"].get("/api/board/personal-bot", headers=_auth(pod))
    ids = {c["id"] for c in default.get_json()["cards"]}
    assert card_id not in ids
    assert default.get_json()["include_archived"] is False

    archived = pod["client"].get("/api/board/personal-bot?include=archived",
                                 headers=_auth(pod))
    assert card_id in {c["id"] for c in archived.get_json()["cards"]}
    assert archived.get_json()["include_archived"] is True
    # The store is untouched — this is a read filter, not a deletion.
    assert bs.find_card(bs.load_board(pod["shared"], "personal-bot"), card_id)


def test_the_two_reads_do_not_share_an_etag(pod):
    """A 304 must never answer the wrong question.

    The ETag is over the SERIALIZED body, so the filtered and unfiltered
    reads cannot collide — pinned because an ETag computed over the stored
    board instead would hand a phone the filtered list for an
    ``?include=archived`` request it had cached.
    """
    a = pod["client"].get("/api/board/personal-bot", headers=_auth(pod))
    b = pod["client"].get("/api/board/personal-bot?include=archived",
                          headers=_auth(pod))
    assert a.headers["ETag"] != b.headers["ETag"]


def test_move_to_dropped_carries_the_reason_through_to_the_event(pod):
    card_id = _card_id(pod, "Pick the trip weekend")
    r = pod["client"].post(f"/api/board/personal-bot/cards/{card_id}/move",
                           json={"to_lane": "dropped", "reason": "not mine"},
                           headers=_auth(pod))
    assert r.status_code == 200
    assert r.get_json()["card"]["drop_reason"] == "not mine"
    rows = []
    events = pod["shared"] / "boards" / "personal-bot" / "events"
    for p in sorted(events.glob("*.jsonl")):
        rows += [json.loads(line) for line in p.read_text().splitlines() if line]
    drop = [e for e in rows if e["event"] == "dropped"][-1]
    assert drop["reason"] == "not mine" and drop["actor"] == "user"


def test_move_refuses_the_retired_bot_lane_by_naming_assign(pod):
    card_id = _card_id(pod, "Pick the trip weekend")
    r = pod["client"].post(f"/api/board/personal-bot/cards/{card_id}/move",
                           json={"to_lane": "bot"}, headers=_auth(pod))
    assert r.status_code == 400
    assert "assign" in r.get_json()["error"]


def test_a_bad_reason_is_a_400_and_writes_nothing(pod):
    card_id = _card_id(pod, "Pick the trip weekend")
    r = pod["client"].post(f"/api/board/personal-bot/cards/{card_id}/move",
                           json={"to_lane": "dropped", "reason": "meh"},
                           headers=_auth(pod))
    assert r.status_code == 400
    card = bs.find_card(bs.load_board(pod["shared"], "personal-bot"), card_id)
    assert card["lane"] == "today"


def test_assign_to_the_bot_makes_an_offer(pod):
    card_id = _card_id(pod, "Pick the trip weekend")
    r = pod["client"].post(f"/api/board/personal-bot/cards/{card_id}/assign",
                           json={"owner": "bot"}, headers=_auth(pod))
    assert r.status_code == 200
    card = r.get_json()["card"]
    # The card keeps its WHEN and gains an offer — the whole point of D-BI7.
    assert card["owner"] == "bot" and card["lane"] == "today"
    assert card["delegation"]["state"] == "offered"

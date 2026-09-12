"""Tests for the BOT's half of the board — ``/api/board-bot/*`` (D-MB4).

WHAT THESE PIN:
  * **A bot can only ever reach its own board.** Identity is the kernel peer
    uid; there is no bot id in the path, the query or the body, so a bot
    cannot name another bot's board even by trying. TCP callers and
    unrecognized uids get 403 — a browser session must never act as a bot.
  * **Each verb's request shape**, end to end against the real store: add
    (with and without enrichment), move, assign, progress.
  * **Enrichment round-trips on create and read, and malformed enrichment is
    refused** — every field must name its source, and the card must be
    unchanged when the block is bad (validate before append).
  * **`board.list` is bounded on the wire** — a 200-card fixture cannot
    return more than the cap, whatever limit the caller asks for.
  * **`progress` refuses a card the bot does not own** — progress is a report
    on a hand-over, and there is no hand-over until someone made one.

Technique mirrors test_google_bot_routes.py: a Flask test client with
REMOTE_TRANSPORT / REMOTE_PEER_UID environ overrides simulating peer
credentials, and the bot's macOS account set to the *current* test user so
the REAL resolver maps os.getuid() back to that bot end to end.
"""
from __future__ import annotations

import json
import os
import pwd
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import board_store as bs  # noqa: E402
from evolve_admin.web.board_bot_routes import (  # noqa: E402
    MAX_LIST_LIMIT, register_board_bot_routes,
)

_ME = pwd.getpwuid(os.getuid()).pw_name

#: The calling bot. ``other-bot``'s account is deliberately unprovisioned, so
#: it never appears in the uid map and cannot be reached from this test's uid.
BOT = "personal-bot"
OTHER = "other-bot"


@pytest.fixture()
def pod(tmp_path: Path):
    shared = tmp_path / "evolve"
    shared.mkdir()
    network = tmp_path / "network.json"
    network.write_text(json.dumps({
        "sharedDir": str(shared),
        "bots": {
            BOT: {"role": "member", "user": _ME},
            OTHER: {"role": "member", "user": "no_such_account_xyz"},
        },
    }))
    app = Flask(__name__)
    register_board_bot_routes(app, network)
    return {"client": app.test_client(), "shared": shared, "app": app}


def _unix_env(uid: int | None = None) -> dict:
    return {"REMOTE_TRANSPORT": "unix-socket",
            "REMOTE_PEER_UID": os.getuid() if uid is None else uid}


def _add(pod, **body):
    body.setdefault("title", "Book the scan")
    body.setdefault("cluster", "health")
    return pod["client"].post("/api/board-bot/cards", json=body,
                              environ_overrides=_unix_env())


def _cards(pod, bot_id: str = BOT):
    return bs.load_board(pod["shared"], bot_id)["cards"]


# ── identity: the bot reaches its own board and no other ──────────────────────


def test_tcp_and_unknown_uid_are_refused(pod):
    """No peer credentials, no board. The admin UI's TCP binding is a browser
    session; an unrecognized uid is not a bot. Both 403 on every verb."""
    c = pod["client"]
    for method, path, body in (
        ("get", "/api/board-bot/cards", None),
        ("post", "/api/board-bot/cards", {"title": "x", "cluster": "admin"}),
        ("post", "/api/board-bot/cards/abc/move", {"to_lane": "today"}),
        ("post", "/api/board-bot/cards/abc/assign", {"owner": "bot"}),
        ("post", "/api/board-bot/cards/abc/progress", {"state": "accepted"}),
    ):
        call = getattr(c, method)
        assert call(path, json=body).status_code == 403, path
        assert call(path, json=body,
                    environ_overrides=_unix_env(4_242_424)).status_code == 403, path


def test_a_bot_cannot_name_another_bots_board(pod):
    """There is no field in which to name one — and the routes carry no bot
    segment — so a body that tries to redirect the write lands on the
    CALLER's board. Cross-bot reach is closed by construction, not by a check
    that could be forgotten."""
    r = _add(pod, title="Mine", bot_id=OTHER, bot=OTHER, board=OTHER)
    assert r.status_code == 201, r.get_json()
    assert [c["title"] for c in _cards(pod, BOT)] == ["Mine"]
    assert _cards(pod, OTHER) == []
    # And no route exists that would accept the other bot's id as a segment.
    rules = {r.rule for r in pod["app"].url_map.iter_rules()}
    assert not any("<bot" in rule for rule in rules), rules


# ── the verbs ─────────────────────────────────────────────────────────────────


def test_add_defaults_and_records_the_bot_as_actor(pod):
    r = _add(pod)
    assert r.status_code == 201
    card = r.get_json()["card"]
    assert card["lane"] == "inbox" and card["owner"] == "me"
    assert card["source"] == "bot"
    rows = [json.loads(line)
            for f in (pod["shared"] / "boards" / BOT / "events").glob("*.jsonl")
            for line in f.read_text().splitlines()]
    assert rows[-1]["event"] == "stocked" and rows[-1]["actor"] == "bot"


def test_move_assign_and_progress_round_trip(pod):
    c = pod["client"]
    card_id = _add(pod).get_json()["card"]["id"]

    r = c.post(f"/api/board-bot/cards/{card_id}/move",
               json={"to_lane": "today"}, environ_overrides=_unix_env())
    assert r.status_code == 200 and r.get_json()["card"]["lane"] == "today"

    r = c.post(f"/api/board-bot/cards/{card_id}/assign",
               json={"owner": "bot"}, environ_overrides=_unix_env())
    assert r.status_code == 200
    assert r.get_json()["card"]["delegation"]["state"] == "offered"

    r = c.post(f"/api/board-bot/cards/{card_id}/progress",
               json={"state": "in_progress", "note": "called, on hold",
                     "cost_to_date": 0.12},
               environ_overrides=_unix_env())
    assert r.status_code == 200
    delegation = r.get_json()["card"]["delegation"]
    assert delegation["state"] == "in_progress"
    assert delegation["progress_note"] == "called, on hold"
    assert delegation["cost_to_date"] == 0.12


def test_progress_refuses_a_card_the_bot_does_not_own(pod):
    """The delegation lifecycle has one entry point: someone hands the card
    over. Reporting progress on a card nobody handed over would let the bot
    invent a delegation it was never offered."""
    card_id = _add(pod).get_json()["card"]["id"]
    r = pod["client"].post(f"/api/board-bot/cards/{card_id}/progress",
                           json={"state": "done"},
                           environ_overrides=_unix_env())
    assert r.status_code == 400
    assert "not assigned to the bot" in r.get_json()["error"]


def test_bad_lane_owner_and_state_are_refused_by_name(pod):
    c = pod["client"]
    card_id = _add(pod).get_json()["card"]["id"]
    # The retired Bot lane is not a lane any more (D-BI7) — and the refusal
    # names the verb that still does what the caller meant, so a model reading
    # it reaches for `assign` rather than concluding hand-over is gone.
    r = c.post(f"/api/board-bot/cards/{card_id}/move", json={"to_lane": "bot"},
               environ_overrides=_unix_env())
    assert r.status_code == 400
    assert "assign" in r.get_json()["error"]
    assert "owner" in r.get_json()["error"]
    # A lane that was never a lane still gets the plain refusal.
    r = c.post(f"/api/board-bot/cards/{card_id}/move", json={"to_lane": "soon"},
               environ_overrides=_unix_env())
    assert r.status_code == 400 and "invalid lane" in r.get_json()["error"]
    r = c.post(f"/api/board-bot/cards/{card_id}/assign", json={"owner": "nobody"},
               environ_overrides=_unix_env())
    assert r.status_code == 400 and "invalid owner" in r.get_json()["error"]
    r = c.post(f"/api/board-bot/cards/{card_id}/progress", json={"state": "nope"},
               environ_overrides=_unix_env())
    assert r.status_code == 400 and "invalid state" in r.get_json()["error"]


def test_unknown_card_is_a_404(pod):
    r = pod["client"].post("/api/board-bot/cards/0123456789abcdef/move",
                           json={"to_lane": "today"},
                           environ_overrides=_unix_env())
    assert r.status_code == 404


def test_an_id_prefix_names_a_card_and_an_ambiguous_one_refuses(pod):
    """Full ids are 32 hex chars; a list line shows 8. A prefix resolves, and
    a prefix matching two cards is an ERROR rather than a coin flip."""
    c = pod["client"]
    card_id = _add(pod).get_json()["card"]["id"]
    r = c.post(f"/api/board-bot/cards/{card_id[:8]}/move",
               json={"to_lane": "later"}, environ_overrides=_unix_env())
    assert r.status_code == 200 and r.get_json()["card"]["lane"] == "later"

    board = bs.load_board(pod["shared"], BOT)
    twin = bs.add_card(board, title="Twin", cluster="admin")
    twin["id"] = card_id[:8] + "f" * (len(card_id) - 8)
    bs.save_board(pod["shared"], BOT, board)
    r = c.post(f"/api/board-bot/cards/{card_id[:8]}/move",
               json={"to_lane": "today"}, environ_overrides=_unix_env())
    assert r.status_code == 400 and "matches 2 cards" in r.get_json()["error"]


# ── enrichment (the 2026-09-01 addendum) ──────────────────────────────────────


def test_enrichment_round_trips_on_create_and_read(pod):
    r = _add(pod, title="Dune, Part Two", cluster="watch", enrichment={
        "runtime_min": {"value": 166, "source": "tmdb"},
        "why_saved": {"value": "you said you wanted something long",
                      "source": "chat", "captured_at": "2026-09-01T10:00:00Z"},
    })
    assert r.status_code == 201
    card = r.get_json()["card"]
    assert card["enrichment"]["runtime_min"]["value"] == 166
    assert card["enrichment"]["runtime_min"]["source"] == "tmdb"
    # Absent captured_at is stamped, present captured_at is preserved.
    assert card["enrichment"]["runtime_min"]["captured_at"].endswith("Z")
    assert card["enrichment"]["why_saved"]["captured_at"] == "2026-09-01T10:00:00Z"

    on_disk = _cards(pod)[0]["enrichment"]
    assert on_disk == card["enrichment"]

    listed = pod["client"].get("/api/board-bot/cards",
                               environ_overrides=_unix_env()).get_json()
    # The list carries the KEYS, not the values — a list is for choosing.
    assert listed["cards"][0]["enriched"] == ["runtime_min", "why_saved"]
    assert "enrichment" not in listed["cards"][0]


@pytest.mark.parametrize("bad", [
    "not-an-object",
    {"runtime_min": 166},                                  # field not an object
    {"runtime_min": {"value": 166}},                       # no source
    {"runtime_min": {"value": 166, "source": "  "}},       # blank source
    {"runtime_min": {"source": "tmdb"}},                   # no value
    {"runtime_min": {"value": 1, "source": "x", "who": "y"}},  # unknown key
    {"Runtime Min!": {"value": 1, "source": "x"}},         # bad field name
])
def test_malformed_enrichment_is_refused_and_adds_nothing(pod, bad):
    r = _add(pod, enrichment=bad)
    assert r.status_code == 400, r.get_json()
    # Validate-before-append: the board must be untouched, not left holding a
    # card whose enrichment was rejected.
    assert _cards(pod) == []


def test_enrichment_is_bounded(pod):
    too_many = {f"f{i}": {"value": i, "source": "x"}
                for i in range(bs.MAX_ENRICHMENT_FIELDS + 1)}
    assert _add(pod, enrichment=too_many).status_code == 400
    too_big = {"why_saved": {"value": "x" * (bs.MAX_ENRICHMENT_CHARS + 1),
                             "source": "chat"}}
    assert _add(pod, enrichment=too_big).status_code == 400
    assert _cards(pod) == []


# ── list: bounded, filterable ─────────────────────────────────────────────────


def _fixture_200(pod):
    board = bs.load_board(pod["shared"], BOT)
    for i in range(200):
        bs.add_card(board, title=f"Card {i}", cluster="work" if i % 2 else "home",
                    lane="today" if i % 3 else "inbox",
                    owner="bot" if i % 5 == 0 else "me")
    bs.save_board(pod["shared"], BOT, board)


def test_list_is_capped_however_much_the_caller_asks_for(pod):
    _fixture_200(pod)
    body = pod["client"].get("/api/board-bot/cards?limit=100000",
                             environ_overrides=_unix_env()).get_json()
    assert body["total"] == 200                 # the honest count
    assert len(body["cards"]) == MAX_LIST_LIMIT  # the bounded payload
    assert body["cap"] == MAX_LIST_LIMIT


def test_list_filters_compose_and_reject_bad_values(pod):
    _fixture_200(pod)
    c = pod["client"]
    body = c.get("/api/board-bot/cards?cluster=work&lane=today&owner=me",
                 environ_overrides=_unix_env()).get_json()
    assert body["cards"], "the fixture should produce some of these"
    for card in body["cards"]:
        assert card["cluster"] == "work"
        assert card["lane"] == "today"
        assert card["owner"] == "me"
    assert c.get("/api/board-bot/cards?lane=bot",
                 environ_overrides=_unix_env()).status_code == 400
    assert c.get("/api/board-bot/cards?owner=someone",
                 environ_overrides=_unix_env()).status_code == 400
    assert c.get("/api/board-bot/cards?limit=abc",
                 environ_overrides=_unix_env()).status_code == 400


def test_list_rows_carry_only_the_choosing_fields(pod):
    _add(pod, note="a long note the model does not need to choose a card")
    row = pod["client"].get("/api/board-bot/cards",
                            environ_overrides=_unix_env()).get_json()["cards"][0]
    assert set(row) == {"id", "title", "lane", "owner", "cluster"}


# ── D-BI2 / D-BI7 on the bot's half ───────────────────────────────────────

def test_list_carries_owner_and_the_dropped_lane(pod):
    c = pod["client"]
    mine = _add(pod, title="Mine").get_json()["card"]["id"]
    theirs = _add(pod, title="Theirs", owner="bot").get_json()["card"]["id"]
    gone = _add(pod, title="Gone").get_json()["card"]["id"]
    c.post(f"/api/board-bot/cards/{gone}/move",
           json={"to_lane": "dropped", "reason": "never"},
           environ_overrides=_unix_env())

    rows = c.get("/api/board-bot/cards",
                 environ_overrides=_unix_env()).get_json()["cards"]
    by_id = {r["id"]: r for r in rows}
    # Owner is on every row, explicitly — a missing field would read to the
    # model as "unknown" when the answer is always one of two things.
    assert by_id[mine]["owner"] == "me"
    assert by_id[theirs]["owner"] == "bot"
    assert by_id[theirs]["delegation"] == "offered"
    assert by_id[gone]["lane"] == "dropped"
    # …and the new lane is a filterable one.
    only = c.get("/api/board-bot/cards?lane=dropped",
                 environ_overrides=_unix_env()).get_json()
    assert [r["id"] for r in only["cards"]] == [gone]


def test_the_bot_sees_the_same_archive_default_as_the_phone(pod):
    from datetime import datetime, timedelta, timezone
    c = pod["client"]
    old = _add(pod, title="Settled last quarter").get_json()["card"]["id"]
    c.post(f"/api/board-bot/cards/{old}/move", json={"to_lane": "done"},
           environ_overrides=_unix_env())
    board = bs.load_board(pod["shared"], BOT)
    bs.find_card(board, old)["settled_at"] = (
        datetime.now(timezone.utc) - timedelta(days=90)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    bs.save_board(pod["shared"], BOT, board)

    default = c.get("/api/board-bot/cards",
                    environ_overrides=_unix_env()).get_json()
    assert old not in {r["id"] for r in default["cards"]}
    assert default["total"] == 0
    full = c.get("/api/board-bot/cards?include=archived",
                 environ_overrides=_unix_env()).get_json()
    assert old in {r["id"] for r in full["cards"]}


def test_a_bot_drop_records_its_reason_like_the_phones(pod):
    c = pod["client"]
    card_id = _add(pod, title="Standup", source="calendar").get_json()["card"]["id"]
    r = c.post(f"/api/board-bot/cards/{card_id}/move",
               json={"to_lane": "dropped", "reason": "already handled"},
               environ_overrides=_unix_env())
    assert r.status_code == 200
    rows = []
    for p in sorted((pod["shared"] / "boards" / BOT / "events").glob("*.jsonl")):
        rows += [json.loads(line) for line in p.read_text().splitlines() if line]
    drop = [e for e in rows if e["event"] == "dropped"][-1]
    assert drop["reason"] == "already handled" and drop["actor"] == "bot"


def test_add_records_the_upstream_source_id_for_dedup(pod):
    card = _add(pod, title="Standup", source="calendar",
                source_id="evt-42").get_json()["card"]
    assert card["source_id"] == "evt-42"
    board = bs.load_board(pod["shared"], BOT)
    # Which is what makes "never show me this again" survive a re-render of
    # the same calendar entry (D-BI2).
    assert bs.card_stocking_identity(board["cards"][0]) == "src:evt-42"


def test_stocking_a_card_the_user_already_dropped_is_skipped(pod):
    c = pod["client"]
    first = _add(pod, title="Standup", source="calendar",
                 source_id="evt-42").get_json()["card"]["id"]
    c.post(f"/api/board-bot/cards/{first}/move",
           json={"to_lane": "dropped", "reason": "never"},
           environ_overrides=_unix_env())

    again = _add(pod, title="Standup", source="calendar", source_id="evt-42")
    assert again.status_code == 200, "a skip is not an error — nothing failed"
    assert again.get_json()["skipped"] == "already settled"
    assert again.get_json()["card"]["id"] == first
    # The board did not grow: a dismissed calendar card never returns.
    assert len(_cards(pod)) == 1

    # A different event is admitted.
    fresh = _add(pod, title="Retro", source="calendar", source_id="evt-43")
    assert fresh.status_code == 201
    assert len(_cards(pod)) == 2


def test_a_card_with_no_upstream_id_is_never_silently_swallowed(pod):
    """Dedup is for STOCKING, which is what an upstream id identifies.

    A card the user (or the bot, on their behalf) types twice is a deliberate
    act; refusing the second one on a title match would make the board lie
    about what was asked for.
    """
    c = pod["client"]
    first = _add(pod, title="Call the plumber").get_json()["card"]["id"]
    c.post(f"/api/board-bot/cards/{first}/move", json={"to_lane": "dropped"},
           environ_overrides=_unix_env())
    assert _add(pod, title="Call the plumber").status_code == 201
    assert len(_cards(pod)) == 2


def test_dedup_reaches_past_the_30_day_tile_filter(pod):
    from datetime import datetime, timedelta, timezone
    c = pod["client"]
    old = _add(pod, title="Weekly sync", source="calendar",
               source_id="evt-7").get_json()["card"]["id"]
    c.post(f"/api/board-bot/cards/{old}/move",
           json={"to_lane": "dropped", "reason": "never"},
           environ_overrides=_unix_env())
    board = bs.load_board(pod["shared"], BOT)
    bs.find_card(board, old)["settled_at"] = (
        datetime.now(timezone.utc) - timedelta(days=365)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    bs.save_board(pod["shared"], BOT, board)
    # Off the board for a year, and still not coming back.
    again = _add(pod, title="Weekly sync", source="calendar", source_id="evt-7")
    assert again.get_json()["skipped"] == "already settled"
    assert len(_cards(pod)) == 1


def test_a_non_string_reason_or_source_id_is_a_400_not_a_500(pod):
    """Malformed fields refuse the same way as their neighbours.

    ``{"reason": ["never"]}`` reached ``.strip()`` and raised — a 500 for the
    same class of mistake that already got a clean 400 on ``to_lane``.
    """
    c = pod["client"]
    card_id = _add(pod, title="Standup").get_json()["card"]["id"]
    r = c.post(f"/api/board-bot/cards/{card_id}/move",
               json={"to_lane": "dropped", "reason": ["never"]},
               environ_overrides=_unix_env())
    assert r.status_code == 400, r.get_json()
    assert "reason must be a string" in r.get_json()["error"]
    # And nothing moved.
    assert bs.find_card(bs.load_board(pod["shared"], BOT), card_id)["lane"] \
        == "inbox"

    r = c.post("/api/board-bot/cards",
               json={"title": "x", "cluster": "home", "source_id": 42},
               environ_overrides=_unix_env())
    assert r.status_code == 400
    assert "source_id must be a string" in r.get_json()["error"]
    assert len(_cards(pod)) == 1, "the refused add wrote nothing"


def test_the_dedup_check_and_the_write_share_one_lock(pod, monkeypatch):
    """Read-then-write across the lock boundary is a check that can go stale.

    Two stocking runs posting the same calendar event could both read "not a
    duplicate" and both write — the exact double-add the dedup exists to
    stop. Pinned by asserting the check happens with the lock HELD, since a
    timing test for this race is inherently flaky.
    """
    from evolve_admin import board_store as store
    from evolve_admin.web import board_bot_routes as routes

    held: list[bool] = []
    real = routes.find_settled_duplicate

    def spy(*a, **k):
        # `threading.Lock` has no owner query; acquire(False) is the portable
        # way to ask "is it already held?" — False means someone holds it,
        # and in this call chain that someone is us.
        free = store.WRITE_LOCK.acquire(blocking=False)
        held.append(not free)
        if free:
            store.WRITE_LOCK.release()
        return real(*a, **k)

    monkeypatch.setattr(routes, "find_settled_duplicate", spy)
    _add(pod, title="Standup", source="calendar", source_id="evt-42")
    assert held == [True], "the duplicate check ran outside the write lock"

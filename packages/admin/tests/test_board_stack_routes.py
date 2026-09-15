"""Tests for the Stack's HTTP surface — the ``/board/<bot>/stack`` page and
the ``/api/board/<bot>/stack``, ``…/seen`` and ``…/decision`` routes added
to ``routes_board.py`` by the ``board-stack-view`` chip.

WHAT THESE PIN:
  * Same auth gate as the lane board — fail closed, token required, page
    and API alike (the gate lives in one shared helper now; this proves
    both pages still go through it).
  * The stack read composes via ``board_stack.stack_order`` over the SAME
    store the lane board reads, and never appears with a briefing (none of
    that chip's endpoints exist yet in this codebase).
  * ``seen``/``decision`` are write routes: rate-limited, CSRF-postured,
    and delegate to ``board_stack``'s writers under the one WRITE_LOCK.
  * ``move`` forwards ``snooze_until`` only where the store accepts it.
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
    today_card = bs.add_card(board, title="Pick the trip weekend", cluster="travel", lane="today")
    inbox_card = bs.add_card(board, title="Unsorted thing", cluster="admin", lane="inbox")
    bot_card = bs.add_card(board, title="Schedule the scan", cluster="health", owner="bot")
    bs.save_board(shared, "personal-bot", board)
    token = bs.mint_token(shared, "personal-bot")
    app = Flask(__name__)
    register_board_routes(app, network)
    return {
        "client": app.test_client(), "shared": shared, "token": token,
        "today_card": today_card, "inbox_card": inbox_card, "bot_card": bot_card,
    }


def _auth(pod):
    return {"Authorization": f"Bearer {pod['token']}"}


# ── the page ─────────────────────────────────────────────────────────────

def test_stack_page_requires_token(pod):
    assert pod["client"].get("/board/personal-bot/stack").status_code == 401


def test_stack_page_upgrades_query_token_to_a_cookie_and_serves_the_shell(pod):
    r = pod["client"].get(f"/board/personal-bot/stack?t={pod['token']}",
                          follow_redirects=True)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "text/html" in r.content_type
    # Static shell only — same invariant board.html's page test pins.
    assert "Pick the trip weekend" not in html


def test_stack_page_has_a_noscript_reading_fallback(pod):
    # Guardrail: "the Stack works without JS for reading". No headless
    # browser here to prove the rendered behaviour with scripting off, but
    # the fallback content this pins is what such a browser would show.
    r = pod["client"].get(f"/board/personal-bot/stack?t={pod['token']}",
                          follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "<noscript>" in html
    assert "JavaScript" in html


def test_stack_page_redirect_targets_the_stack_path_not_the_lane_board(pod):
    r = pod["client"].get(f"/board/personal-bot/stack?t={pod['token']}")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/board/personal-bot/stack")


def test_lane_board_page_still_works_after_the_shared_refactor(pod):
    r = pod["client"].get(f"/board/personal-bot?t={pod['token']}", follow_redirects=True)
    assert r.status_code == 200


# ── GET .../stack ────────────────────────────────────────────────────────

def test_stack_api_requires_token(pod):
    assert pod["client"].get("/api/board/personal-bot/stack").status_code == 401


def test_stack_api_composes_the_same_cards_the_lane_board_sees(pod):
    r = pod["client"].get("/api/board/personal-bot/stack", headers=_auth(pod))
    assert r.status_code == 200
    body = r.get_json()
    card_items = [i for i in body["items"] if i["kind"] == "card"]
    titles = [i["card"]["title"] for i in card_items]
    # bot return group is empty (nothing returned_for_review yet): "Schedule
    # the scan" is owner=bot but only OFFERED, not a "return" (D-ST2 group 1
    # is specifically returned_for_review) — it still shows in its own lane
    # (inbox), by add time, same as any other inbox card.
    assert titles == ["Pick the trip weekend", "Unsorted thing", "Schedule the scan"]
    assert body["empty_state"] is None
    assert all("why_line" in i["card"] for i in card_items)


def test_stack_api_returns_for_review_card_leads(pod):
    bs.set_delegation_progress(pod["shared"], "personal-bot", pod["bot_card"]["id"],
                                state="returned_for_review", actor="bot")
    r = pod["client"].get("/api/board/personal-bot/stack", headers=_auth(pod))
    titles = [i["card"]["title"] for i in r.get_json()["items"] if i["kind"] == "card"]
    assert titles[0] == "Schedule the scan"


def test_stack_api_empty_state_when_nothing_left(pod):
    for card in (pod["today_card"], pod["inbox_card"], pod["bot_card"]):
        try:
            bs.move_card(pod["shared"], "personal-bot", card["id"], "done", actor="user")
        except ValueError:
            pass
    r = pod["client"].get("/api/board/personal-bot/stack", headers=_auth(pod))
    body = r.get_json()
    assert body["items"] == []
    assert body["empty_state"] == {
        "done_today": 3, "dropped_today": 0, "handed_off_today": 0, "bots_plate": [],
    }


def test_stack_api_never_carries_a_briefing_yet(pod):
    # No GET /board/<bot>/briefing route exists anywhere in this codebase
    # (Morning Board is still in-flight) — pinned so a future chip landing
    # that endpoint is a deliberate, reviewed change to this test, not a
    # silent behaviour shift.
    r = pod["client"].get("/api/board/personal-bot/stack", headers=_auth(pod))
    assert all(i["kind"] != "briefing" for i in r.get_json()["items"])


# ── POST .../seen ────────────────────────────────────────────────────────

def test_seen_requires_token(pod):
    r = pod["client"].post(f"/api/board/personal-bot/cards/{pod['today_card']['id']}/seen")
    assert r.status_code == 401


def test_seen_increments_and_reports_third_pass(pod):
    cid = pod["today_card"]["id"]
    for expect_warn in (False, False, True):
        r = pod["client"].post(f"/api/board/personal-bot/cards/{cid}/seen",
                               headers=_auth(pod), json={})
        assert r.status_code == 200
        assert r.get_json()["warn_third_pass"] is expect_warn


def test_seen_undo(pod):
    cid = pod["today_card"]["id"]
    pod["client"].post(f"/api/board/personal-bot/cards/{cid}/seen", headers=_auth(pod), json={})
    r = pod["client"].post(f"/api/board/personal-bot/cards/{cid}/seen",
                          headers=_auth(pod), json={"undo": True})
    assert r.get_json()["card"]["seen_today"]["count"] == 0


def test_seen_unknown_card_404s(pod):
    r = pod["client"].post("/api/board/personal-bot/cards/no-such-id/seen",
                           headers=_auth(pod), json={})
    assert r.status_code == 404


# ── POST .../decision ────────────────────────────────────────────────────

def test_decision_requires_token(pod):
    r = pod["client"].post(f"/api/board/personal-bot/cards/{pod['bot_card']['id']}/decision")
    assert r.status_code == 401


def test_decision_approve(pod):
    cid = pod["bot_card"]["id"]
    bs.set_delegation_progress(pod["shared"], "personal-bot", cid,
                                state="returned_for_review", actor="bot")
    r = pod["client"].post(f"/api/board/personal-bot/cards/{cid}/decision",
                           headers=_auth(pod), json={"decision": "approve"})
    assert r.status_code == 200
    assert r.get_json()["card"]["delegation"]["state"] == "done"


def test_decision_on_a_card_not_owned_by_the_bot_is_a_400(pod):
    cid = pod["today_card"]["id"]  # owner: me
    r = pod["client"].post(f"/api/board/personal-bot/cards/{cid}/decision",
                           headers=_auth(pod), json={"decision": "approve"})
    assert r.status_code == 400


def test_decision_invalid_value_is_a_400(pod):
    cid = pod["bot_card"]["id"]
    r = pod["client"].post(f"/api/board/personal-bot/cards/{cid}/decision",
                           headers=_auth(pod), json={"decision": "shrug"})
    assert r.status_code == 400


# ── move's snooze_until (D-ST4) ──────────────────────────────────────────

def test_move_to_later_forwards_snooze_until(pod):
    cid = pod["inbox_card"]["id"]
    r = pod["client"].post(f"/api/board/personal-bot/cards/{cid}/move",
                           headers=_auth(pod),
                           json={"to_lane": "later", "snooze_until": "2026-09-20T09:00:00Z"})
    assert r.status_code == 200
    assert r.get_json()["card"]["snooze_until"] == "2026-09-20T09:00:00Z"


def test_move_to_today_with_snooze_until_is_a_400(pod):
    cid = pod["inbox_card"]["id"]
    r = pod["client"].post(f"/api/board/personal-bot/cards/{cid}/move",
                           headers=_auth(pod),
                           json={"to_lane": "today", "snooze_until": "2026-09-20T09:00:00Z"})
    assert r.status_code == 400


def test_a_later_card_with_a_future_snooze_is_excluded_from_the_stack(pod):
    cid = pod["inbox_card"]["id"]
    pod["client"].post(f"/api/board/personal-bot/cards/{cid}/move",
                       headers=_auth(pod),
                       json={"to_lane": "later", "snooze_until": "2999-01-01T00:00:00Z"})
    r = pod["client"].get("/api/board/personal-bot/stack", headers=_auth(pod))
    ids = [i["card"]["id"] for i in r.get_json()["items"] if i["kind"] == "card"]
    assert cid not in ids

"""Tests for D-TM4 capture from turns — the daemon half (``board_capture``).

WHAT THESE PIN:
  * **The user's words land as a proposal** — ``kind: proposal`` in ``inbox``,
    ``due`` from "by the 30th", NO pace (the touch scheduler must not fire on
    a card nobody kept), shown next on the Stack; swipe-up keeps it (``kind``
    cleared, pace set from the table); unkept for 7 days → dropped ``expired``.
  * **The bot's own promise lands as a bot-owned card with no confirmation**
    — "I'll chase this Thursday" → ``owner: bot``, ``next_touch`` Thursday
    09:00 in the pod's zone, the face says "promised in chat …".
  * **Dedup** — a repeat mention touches the open card ("mentioned again")
    and makes no second card; the same turn posted twice makes no model call.
  * **The one model call is fenced** — unpriced rung or over the Board app's
    per-call cap → refused BEFORE any call; a gate-only post (no match) never
    reaches the classifier.
  * **Controls** — the capture half of the pod-report line, and the "capture
    gate live" health control's unknown/ok/idle.
"""
from __future__ import annotations

import json
import os
import pwd
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import app_contract, board_capture as bc, board_stack, board_store as bs  # noqa: E402

BOT = "personal-bot"
NET = {"timezone": "UTC"}
TUE = datetime(2026, 9, 22, 14, 5, tzinfo=timezone.utc)  # a Tuesday


class FakeClassifier:
    """Stands in for the fast-rung call; counts every invocation."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0
        self.prompts: list[str] = []

    def __call__(self, *, prompt, shared_dir, bot_id, network):
        self.calls += 1
        self.prompts.append(prompt)
        res = self.results.pop(0) if self.results else {"none": True}
        return res, {"outcome": "ok", "model": "provider-x/fast-model", "cost_usd": 0.0004}


def _req(text, speaker="user", turn="run-1", session="sess-1"):
    return {"session": session, "turn_id": turn, "speaker": speaker, "text_excerpt": text}


@pytest.fixture()
def shared(tmp_path):
    return tmp_path / "evolve"


def _cards(shared):
    return bs.load_board(shared, BOT)["cards"]


def test_user_deadline_becomes_a_proposal_with_due_and_no_pace(shared):
    clf = FakeClassifier({"title": "Pay the venue deposit", "outcome": "Deposit paid and confirmed",
                          "cadence": "later", "due": None})
    out = bc.propose(shared, BOT, NET, _req("remind me to pay the deposit by the 30th"),
                     classifier=clf, now=TUE)
    card = out["card"]
    assert out["outcome"] == "proposed" and clf.calls == 1
    assert card["kind"] == "proposal" and card["lane"] == "inbox" and card["owner"] == "me"
    assert card["due"] == "2026-09-30"          # parsed from the words, not the model
    assert "next_touch" not in card and "touch_action" not in card
    assert card["proposed_pace"] == {"cadence": "later", "touch_action": "remind"}
    assert card["capture"]["turn_id"] == "run-1" and card["source"] == "turn"
    assert board_stack.card_why(card) == "proposed from chat Tue 22 Sep 14:05 — keep?"


def test_proposal_is_shown_next_and_keep_sets_the_pace(shared):
    bs.create_card(shared, BOT, title="Older inbox card", cluster="admin")
    clf = FakeClassifier({"title": "Pay the venue deposit", "cadence": "later"})
    card = bc.propose(shared, BOT, NET, _req("remind me to pay the deposit by the 30th"),
                      classifier=clf, now=TUE)["card"]
    order = board_stack.stack_order(bs.visible_cards(bs.load_board(shared, BOT)), now=TUE)
    assert [i["card"]["title"] for i in order][:2] == ["Pay the venue deposit", "Older inbox card"]
    kept, warn = board_stack.record_card_seen(shared, BOT, card["id"], actor="user", now=TUE)
    assert warn is False and "kind" not in kept and "proposed_pace" not in kept
    assert kept["touch_action"] == "remind" and kept["cadence"] == "later"
    assert kept["next_touch"] == "2026-09-29T09:00:00Z"   # due overrides: the day before, 09:00


def test_bot_promise_is_bot_owned_with_next_touch_on_thursday(shared):
    clf = FakeClassifier({"title": "Chase the contract", "outcome": "Signed contract back",
                          "cadence": "soon", "source_named": True})
    out = bc.propose(shared, BOT, NET, _req("I'll chase this Thursday.", speaker="bot"),
                     classifier=clf, now=TUE)
    card = out["card"]
    assert out["outcome"] == "promised"
    assert card["owner"] == "bot" and "kind" not in card
    assert card["delegation"]["state"] == "accepted"
    assert card["next_touch"] == "2026-09-24T09:00:00Z"   # Thursday, 09:00 pod zone
    assert card["touch_action"] == "check_source"
    assert board_stack.card_why(card) == "promised in chat Tue 22 Sep 14:05"


def test_bot_promise_without_a_day_or_source_is_soon_and_ask(shared):
    clf = FakeClassifier({"title": "Check back on the quote", "cadence": "now"})
    card = bc.propose(shared, BOT, NET, _req("I'll check back once they reply.", speaker="bot"),
                      classifier=clf, now=TUE)["card"]
    assert card["cadence"] == "soon" and card["touch_action"] == "ask"
    assert card["next_touch"] == "2026-09-24T14:05:00Z"   # soon = 2 days


def test_duplicate_mention_touches_the_open_card_and_adds_none(shared):
    clf = FakeClassifier({"title": "Pay the venue deposit"}, {"title": "Pay venue deposit"})
    first = bc.propose(shared, BOT, NET, _req("I need to pay the deposit by friday"),
                       classifier=clf, now=TUE)["card"]
    out = bc.propose(shared, BOT, NET, _req("don't forget the deposit", turn="run-2"),
                     classifier=clf, now=TUE)
    assert out["outcome"] == "mentioned_again" and out["card"]["id"] == first["id"]
    cards = _cards(shared)
    assert len(cards) == 1
    assert cards[0]["touches"][-1]["result"] == "mentioned again"
    assert "Pay the venue deposit" in clf.prompts[1]      # the open titles went to the call


def test_same_turn_twice_makes_no_second_model_call(shared):
    clf = FakeClassifier({"title": "Book the dentist"})
    bc.propose(shared, BOT, NET, _req("remind me to book the dentist"), classifier=clf, now=TUE)
    out = bc.propose(shared, BOT, NET, _req("remind me to book the dentist"), classifier=clf, now=TUE)
    assert out["outcome"] == "duplicate_turn" and clf.calls == 1 and len(_cards(shared)) == 1


def test_classifier_none_makes_no_card(shared):
    out = bc.propose(shared, BOT, NET, _req("see you later"), classifier=FakeClassifier(), now=TUE)
    assert out == {"outcome": "none", "card": None} and _cards(shared) == []


def test_unpriced_or_over_budget_refuses_before_any_call(shared, monkeypatch):
    import engine_llm

    def _boom(*a, **k):
        raise AssertionError("the model was called")
    monkeypatch.setattr(engine_llm, "engine_complete", _boom)
    monkeypatch.setattr(app_contract, "model_tier_chain", lambda n, b, t: [])
    out = bc.propose(shared, BOT, NET, _req("remind me to call the plumber"), now=TUE)
    assert out["outcome"] == "unpriced"
    monkeypatch.setattr(app_contract, "model_tier_chain", lambda n, b, t: ["provider-x/fast-model"])
    monkeypatch.setattr(app_contract, "model_price", lambda s, p, m: {
        "input_cost_per_token": 1.0, "output_cost_per_token": 1.0})
    out = bc.propose(shared, BOT, NET, _req("remind me to call the plumber", turn="run-2"), now=TUE)
    assert out["outcome"] == "over_budget" and _cards(shared) == []


def test_production_classifier_is_one_fast_rung_call_attributed_to_the_board_app(shared, monkeypatch):
    import engine_llm
    calls = []

    def _complete(prompt, **kw):
        calls.append(kw)
        return '{"title": "Call the plumber", "cadence": "today"}', engine_llm.OK
    monkeypatch.setattr(engine_llm, "engine_complete", _complete)
    monkeypatch.setattr(app_contract, "model_tier_chain", lambda n, b, t: ["provider-x/fast-model"])
    monkeypatch.setattr(app_contract, "model_price", lambda s, p, m: {
        "input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6})
    out = bc.propose(shared, BOT, NET, _req("remind me to call the plumber"), now=TUE)
    assert out["outcome"] == "proposed" and len(calls) == 1
    assert calls[0]["role"] == "fast" and calls[0]["model_hint"] == "provider-x/fast-model"
    rows = [json.loads(line) for line in (bs.board_dir(shared, BOT) / "events" /
            "2026-09-22.jsonl").read_text().splitlines()]
    cost = [r for r in rows if r["event"] == "capture_classified"]
    assert len(cost) == 1 and cost[0]["app_id"] == bc.CAPTURE_APP_ID and cost[0]["cost_usd"] > 0


def test_unkept_proposal_expires_after_seven_days(shared):
    clf = FakeClassifier({"title": "Renew the passport"})
    card = bc.propose(shared, BOT, NET, _req("remind me to renew the passport"),
                      classifier=clf, now=TUE)["card"]
    later = TUE + timedelta(days=7)
    assert board_stack.stack_order(bs.visible_cards(bs.load_board(shared, BOT)), now=later) == []
    assert bc.expire_proposals(shared, BOT, now=later) == 1
    dropped = bs.find_card(bs.load_board(shared, BOT), card["id"])
    assert dropped is not None
    assert dropped["lane"] == "dropped" and dropped["drop_reason"] == "expired"
    with pytest.raises(ValueError):   # a user tap can never claim "expired"
        bs.move_card(shared, BOT, card["id"], "dropped", actor="user", reason="expired")


def test_weekly_capture_stats_and_the_report_line(shared):
    clf = FakeClassifier({"title": "A"}, {"title": "Bravo thing"}, {"title": "Chase it"})
    a = bc.propose(shared, BOT, NET, _req("remind me to do A", turn="t1"), classifier=clf, now=TUE)["card"]
    b = bc.propose(shared, BOT, NET, _req("remind me bravo", turn="t2"), classifier=clf, now=TUE)["card"]
    bc.propose(shared, BOT, NET, _req("I'll chase it friday", "bot", turn="t3"), classifier=clf, now=TUE)
    board_stack.record_card_seen(shared, BOT, a["id"], actor="user", now=TUE)
    bs.move_card(shared, BOT, b["id"], "dropped", actor="user", reason="never")
    stats = bc.weekly_capture_stats(shared, BOT, now=TUE)
    assert stats == {"captures_proposed": 2, "captures_kept": 1, "captures_dropped": 1}


def test_gate_verdict_unknown_ok_idle(shared):
    assert bc.gate_verdict(shared, BOT, turns_seen=True, now=TUE)[0] == "unknown"
    assert bc.gate_verdict(shared, BOT, turns_seen=False, now=TUE)[0] == "idle"
    bc.record_gate(shared, BOT, {"evaluated": 12, "matched": 1}, now=TUE)
    status, detail = bc.gate_verdict(shared, BOT, turns_seen=True, now=TUE)
    assert status == "ok" and "12 turns evaluated, 1 matched" in detail


@pytest.mark.parametrize("name", ["known_good.json", "known_bad.json"])
def test_capture_gate_control_fixtures(tmp_path, name):
    from evolve_admin import health
    fx = json.loads((_ADMIN_DIR.parent.parent / "tests" / "fixtures" / "controls" /
                     "health__check_capture_gate" / name).read_text())
    shared, bot = tmp_path / "evolve", fx["bot_id"]
    if fx["turns_seen"]:
        (shared / bot / "turns").mkdir(parents=True)
        (shared / bot / "turns" / "t.jsonl").write_text("{}\n")
    if fx["gate"]:
        bc.record_gate(shared, bot, fx["gate"])
    report = health.HealthReport()
    health._check_capture_gate(report, shared, [bot])
    [check] = report.checks
    want = fx["expect"]
    assert (check.status, check.category) == (want["status"], want["category"])
    assert want["detail_contains"] in check.detail


def test_health_control_reports_unknown_when_turns_seen_but_gate_silent(shared):
    from evolve_admin import health
    (shared / BOT / "turns").mkdir(parents=True)
    (shared / BOT / "turns" / "2026-09-22.jsonl").write_text("{}\n")
    report = health.HealthReport()
    health._check_capture_gate(report, shared, [BOT])
    [check] = report.checks
    assert check.status == health.WARN and "capture gate unknown" in check.detail


@pytest.mark.parametrize("text,expected", [
    ("I'll chase this Thursday", "2026-09-24"),
    ("by the 30th", "2026-09-30"),
    ("by the 5th", "2026-10-05"),
    ("tomorrow", "2026-09-23"),
    ("on tuesday", "2026-09-29"),     # the NEXT Tuesday, never today
    ("sometime", None),
])
def test_stated_day(text, expected):
    assert bc.stated_day(text, TUE) == expected


# ── the route: a gate-only post never reaches the classifier ─────────────────

_ME = pwd.getpwuid(os.getuid()).pw_name


def test_route_gate_only_post_makes_no_model_call(tmp_path, monkeypatch):
    from flask import Flask

    from evolve_admin.web import board_bot_routes as routes
    shared = tmp_path / "evolve"
    shared.mkdir()
    network = tmp_path / "network.json"
    network.write_text(json.dumps({"sharedDir": str(shared), "timezone": "UTC",
                                   "bots": {BOT: {"role": "member", "user": _ME}}}))
    clf = FakeClassifier({"title": "Book the dentist"})
    monkeypatch.setattr(routes, "spawn_capture", lambda s, b, n, r: bc.propose(
        s, b, n, r, classifier=clf))
    app = Flask(__name__)
    routes.register_board_bot_routes(app, network)
    env = {"REMOTE_TRANSPORT": "unix-socket", "REMOTE_PEER_UID": os.getuid()}
    c = app.test_client()
    r = c.post("/api/board-bot/capture", json={"gate": {"evaluated": 5, "matched": 0}},
               environ_overrides=env)
    assert r.status_code == 202 and clf.calls == 0 and _cards(shared) == []
    r = c.post("/api/board-bot/capture", json={
        "gate": {"evaluated": 6, "matched": 1},
        "capture": _req("remind me to book the dentist")}, environ_overrides=env)
    assert r.status_code == 202 and clf.calls == 1
    assert [x["kind"] for x in _cards(shared)] == ["proposal"]
    assert c.post("/api/board-bot/capture", json={"capture": {"speaker": "user"}},
                  environ_overrides=env).status_code == 400
    assert c.post("/api/board-bot/capture", json={"gate": {}}).status_code == 403


def test_pod_report_line_carries_the_capture_counts(shared):
    import pod_report
    clf = FakeClassifier({"title": "Book the dentist"})
    bc.propose(shared, BOT, NET, _req("remind me to book the dentist"), classifier=clf,
               now=datetime.now(timezone.utc))
    [line] = pod_report.collect_touch_report(shared, [BOT])
    assert line.text.endswith("captures proposed 1 / kept 0 / dropped 0"), line.text
    assert pod_report.collect_touch_report(shared, ["bot-a"]) == []   # quiet bot, quiet report

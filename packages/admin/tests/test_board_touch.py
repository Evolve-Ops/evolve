"""Tests for board_touch.py (D-TM3/6/7/10, the daemon's touch scheduler).

Fixture pod throughout — no live pod, no model call anywhere on this
module's own path (the guardrail: ``sweep``/``remind`` never construct or
call a model client; ``do_action``/``check_source``-changed only ever
APPEND the same ``instruction``/``assigned`` events a UI tap produces,
never call ``board_worker.run_delegation`` directly — pinned below by
poisoning it).

WHAT THESE PIN:
  * The sweep fires exactly once per due touch (the ``touch_due`` dedup
    guard) and never touches a model client.
  * ``remind`` composes deterministic text with and without enrichment,
    sends it, and records the touch — no model call.
  * D-TM6: a third due ``remind`` touch (two already recorded, nothing
    else interrupting the streak) fires ``ask`` instead, raising the card
    to ``inbox``.
  * D-TM3: ``check_source`` backs off on "unchanged" and wakes the D-BI4
    worker (an ``assigned`` event, owner -> bot) on "changed".
  * D-TM7: a second consecutive "unchanged" probe on an ``owner: other``
    card adds the drafted-nudge action.
  * D-TM3 item 6: a touch_due with no matching touch within its window is
    ledgered ``missed`` through board_worker's shared ledger, and clears
    on the next fired touch.
  * D-TM10: the weekly report line's numbers match a hand-built fixture.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import board_store as bs  # noqa: E402
from evolve_admin import board_touch as bt  # noqa: E402
from evolve_admin import board_worker as bw  # noqa: E402
from evolve_admin.alerts import dispatcher as alerts_dispatcher  # noqa: E402

BOT = "personal-bot"


def _network() -> dict:
    return {"bots": {BOT: {
        "primary_channel": "telegram",
        "primary_user": {"external_ids": {"telegram": "12345"}},
        "port": 18790,
    }}}


def _refusing_delegation(*a, **kw):  # pragma: no cover — asserted never called
    raise AssertionError("board_touch must never call run_delegation directly")


def _stub_send(monkeypatch, *, ok=True, error=None):
    sent = []

    def fake(bot_id, network, message):
        sent.append({"bot_id": bot_id, "network": network, "message": message})
        return ok, error

    monkeypatch.setattr(alerts_dispatcher, "send_direct_to_bot", fake)
    return sent


def _add_card(tmp_path, *, cluster="fitness", source="manual", **kw) -> dict:
    board = bs.load_board(tmp_path, BOT)
    card = bs.add_card(board, title="Dentist", cluster=cluster, source=source, **kw)
    bs.save_board(tmp_path, BOT, board)
    return card


def _past(now: datetime, minutes: int = 1) -> str:
    return bs._utcnow(now - timedelta(minutes=minutes))  # noqa: SLF001


def _pace(tmp_path, card_id, *, cadence, next_touch, touch_action):
    return bs.set_pace(
        tmp_path, BOT, card_id, cadence=cadence, next_touch=next_touch,
        touch_action=touch_action, actor="user")


def _fresh(tmp_path, card_id) -> dict:
    board = bs.load_board(tmp_path, BOT)
    return bs.find_card(board, card_id)


# ── scheduler + remind ──────────────────────────────────────────────────

def test_sweep_fires_reminder_and_never_touches_a_model_client(tmp_path, monkeypatch):
    monkeypatch.setattr(bw, "run_delegation", _refusing_delegation)
    sent = _stub_send(monkeypatch)
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    card = _add_card(tmp_path, outcome="Confirm the venue deposit")
    _pace(tmp_path, card["id"], cadence="now", next_touch=_past(now), touch_action="remind")

    results = bt.sweep(tmp_path, _network(), now=now)

    assert len(results) == 1
    assert results[0]["result"] == "reminded"
    assert len(sent) == 1
    assert "Dentist" in sent[0]["message"]
    fresh = _fresh(tmp_path, card["id"])
    assert fresh["touches"][-1]["action"] == "remind"
    assert fresh["touches"][-1]["result"] == "reminded"
    # next_touch advanced into the future — not due again this tick.
    assert bs._parse_ts(fresh["next_touch"]) > now  # noqa: SLF001


def test_sweep_does_not_refire_within_the_window(tmp_path, monkeypatch):
    """item 1's own dedup guard: a second tick moments later must not
    double-send while the first touch's window is still open."""
    sent = _stub_send(monkeypatch)
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    card = _add_card(tmp_path)
    _pace(tmp_path, card["id"], cadence="now", next_touch=_past(now), touch_action="remind")

    bt.sweep(tmp_path, _network(), now=now)
    # A card whose next_touch was already advanced won't be due again, but
    # force it stale to prove the guard (not just "not due") is what blocks
    # a second send within the window.
    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    fresh["next_touch"] = _past(now)
    bs.save_board(tmp_path, BOT, board)

    bt.sweep(tmp_path, _network(), now=now + timedelta(minutes=5))
    assert len(sent) == 1  # still just the first send


def test_compose_remind_message_without_enrichment():
    card = {"title": "Dentist", "outcome": "Confirm the appointment", "due": "2026-10-01"}
    text = bt.compose_remind_message(card)
    assert text == "Dentist\nConfirm the appointment\nDue: 2026-10-01"


def test_compose_remind_message_with_enrichment():
    card = {
        "title": "Call the venue",
        "enrichment": {
            "contacts": {"value": [{"name": "Venue", "phone": "555-1000"}], "source": "manual"},
            "location": {"value": {"text": "123 Main St", "maps_url": "https://maps/x"}, "source": "manual"},
            "links": {"value": [{"label": "confirmation", "url": "https://example.com/x"}], "source": "manual"},
        },
    }
    text = bt.compose_remind_message(card)
    assert "Venue · 555-1000" in text
    assert "123 Main St (https://maps/x)" in text
    assert "confirmation: https://example.com/x" in text


def test_third_reminder_becomes_ask(tmp_path, monkeypatch):
    sent = _stub_send(monkeypatch)
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    card = _add_card(tmp_path)
    _pace(tmp_path, card["id"], cadence="now", next_touch=_past(now), touch_action="remind")

    for i in range(3):
        board = bs.load_board(tmp_path, BOT)
        fresh = bs.find_card(board, card["id"])
        fresh["next_touch"] = _past(now)
        bs.save_board(tmp_path, BOT, board)
        # Strictly more than the remind/ask window apart (item 1's own
        # dedup guard blocks a re-fire AT the window boundary, same
        # inclusive convention board_worker.sweep_delivery_windows uses).
        bt.sweep(tmp_path, _network(), now=now + timedelta(hours=2 * i))

    assert len(sent) == 2  # only the first two touches sent a reminder
    fresh = _fresh(tmp_path, card["id"])
    touches = [t["action"] for t in fresh["touches"]]
    assert touches == ["remind", "remind", "ask"]
    assert fresh["lane"] == "inbox"


def test_sweep_reminder_recompute_uses_the_pod_timezone(tmp_path, monkeypatch):
    """D-AP1: the fired touch's ``next_touch`` recompute happens in the
    pod's OWN zone (``config.resolve_pod_timezone`` threaded as ``tz``),
    not hardcoded UTC — the contract this module's docstring claims."""
    _stub_send(monkeypatch)
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)  # 05:00 PDT
    card = _add_card(tmp_path)
    _pace(tmp_path, card["id"], cadence="today", next_touch=_past(now), touch_action="remind")
    network = _network()
    network["timezone"] = "America/Los_Angeles"

    bt.sweep(tmp_path, network, now=now)

    fresh = _fresh(tmp_path, card["id"])
    next_touch = bs._parse_ts(fresh["next_touch"])  # noqa: SLF001
    # 18:00 America/Los_Angeles on 2026-09-22 (PDT, UTC-7) is 01:00 UTC the
    # next day — if this landed on plain UTC instead, it would be 18:00Z.
    assert next_touch == datetime(2026, 9, 23, 1, 0, 0, tzinfo=timezone.utc)


# ── check_source ─────────────────────────────────────────────────────────

def test_check_source_unchanged_backs_off(tmp_path):
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    card = _add_card(tmp_path, cluster="work")
    _pace(tmp_path, card["id"], cadence="soon", next_touch=_past(now), touch_action="check_source")
    probes = {"manual": lambda card, network: "unchanged"}

    results = bt.sweep(tmp_path, _network(), now=now, probes=probes)

    assert results[0]["result"] == "no_change"
    fresh = _fresh(tmp_path, card["id"])
    assert fresh["touches"][-1]["result"] == "no_change"
    # backed off past the plain "soon" first-touch offset (2 days)
    next_touch = bs._parse_ts(fresh["next_touch"])  # noqa: SLF001
    assert next_touch > now + timedelta(days=2)


def test_check_source_changed_wakes_worker(tmp_path):
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    card = _add_card(tmp_path, cluster="work", owner="other",
                      waiting_on={"who": "Alex", "since": "2026-09-01", "source": "email"})
    _pace(tmp_path, card["id"], cadence="soon", next_touch=_past(now), touch_action="check_source")
    probes = {"email": lambda card, network: "changed"}

    results = bt.sweep(tmp_path, _network(), now=now, probes=probes)

    assert results[0]["result"] == "changed"
    fresh = _fresh(tmp_path, card["id"])
    assert fresh["owner"] == "bot"
    assert fresh["delegation"]["state"] == "offered"
    assert "waiting_on" not in fresh
    events = _events(tmp_path)
    assigned = [e for e in events if e["event"] == "assigned"]
    assert assigned and assigned[-1]["to"] == "bot" and "context" in assigned[-1]


def test_check_source_changed_clears_pace_so_it_cannot_refire(tmp_path):
    """The fix for the delegation-clobber bug: once a card is handed to the
    worker, this scheduler must never touch it again while delegation is
    live — otherwise a later ``check_source`` re-fire re-enters
    `_hand_off_to_worker` and clobbers the worker's own progress."""
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    card = _add_card(tmp_path, cluster="work", owner="other",
                      waiting_on={"who": "Alex", "since": "2026-09-01", "source": "email"})
    _pace(tmp_path, card["id"], cadence="soon", next_touch=_past(now), touch_action="check_source")
    probes = {"email": lambda card, network: "changed"}

    bt.sweep(tmp_path, _network(), now=now, probes=probes)

    fresh = _fresh(tmp_path, card["id"])
    assert "touch_action" not in fresh
    assert "next_touch" not in fresh
    assert "cadence" not in fresh


def test_hand_off_to_worker_does_not_clobber_active_delegation(tmp_path):
    """`_hand_off_to_worker`'s own guard (belt to the pace-clear's
    suspenders): a card the worker is already progressing must not be
    reset to ``offered`` — that would erase real state (cost_to_date) and,
    since `instructed` stays set, board_worker's own `assigned` handler
    would then skip the new event outright, stranding the card."""
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    card = _add_card(tmp_path, owner="bot")
    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    fresh["delegation"] = {"state": "in_progress", "cost_to_date": 0.37}
    fresh["instructed"] = {"action_id": "call_ahead", "label": "Call ahead", "est_cost": 0.5}
    bs.save_board(tmp_path, BOT, board)

    bt._hand_off_to_worker(  # noqa: SLF001
        tmp_path, BOT, card["id"], context="check_source: email changed", actor=BOT, now=now)

    fresh = _fresh(tmp_path, card["id"])
    assert fresh["delegation"] == {"state": "in_progress", "cost_to_date": 0.37}
    assert fresh["instructed"]["action_id"] == "call_ahead"
    assert not any(e["event"] == "assigned" for e in _events(tmp_path, card["id"]))


def test_other_owner_second_unchanged_probe_adds_drafted_nudge(tmp_path):
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    # "hobbies" carries only two default actions (board_actions.py) — room
    # left under MAX_ACTIONS_PER_CARD for the nudge; "work" would already
    # be at the cap and the nudge would be silently dropped by design.
    card = _add_card(tmp_path, cluster="hobbies", owner="other",
                      waiting_on={"who": "Alex", "since": "2026-09-01", "source": "email"})
    _pace(tmp_path, card["id"], cadence="soon", next_touch=_past(now), touch_action="check_source")
    probes = {"email": lambda card, network: "unchanged"}

    # first unchanged probe: no nudge yet (D-TM7: "a SECOND unchanged probe").
    bt.sweep(tmp_path, _network(), now=now, probes=probes)
    fresh = _fresh(tmp_path, card["id"])
    assert not any(a.get("id") == bt.NUDGE_ACTION_ID for a in fresh.get("actions") or [])

    fresh["next_touch"] = _past(now)
    board = bs.load_board(tmp_path, BOT)
    bs.find_card(board, card["id"])["next_touch"] = _past(now)
    bs.save_board(tmp_path, BOT, board)
    bt.sweep(tmp_path, _network(), now=now + timedelta(days=1), probes=probes)

    fresh = _fresh(tmp_path, card["id"])
    nudge = [a for a in fresh.get("actions") or [] if a.get("id") == bt.NUDGE_ACTION_ID]
    assert len(nudge) == 1
    assert nudge[0]["act"] == "send_nudge"


def test_check_source_unknown_probe_never_backs_off_or_escalates(tmp_path):
    """deviation 1: the shipped stub probes are honest 'unknown', never a
    fabricated 'unchanged' — D-CS7."""
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    card = _add_card(tmp_path, cluster="work", owner="other",
                      waiting_on={"who": "Alex", "since": "2026-09-01", "source": "email"})
    _pace(tmp_path, card["id"], cadence="soon", next_touch=_past(now), touch_action="check_source")

    results = bt.sweep(tmp_path, _network(), now=now)  # default probes -> "unknown"

    assert results[0]["result"] == "unknown"
    fresh = _fresh(tmp_path, card["id"])
    assert not any(a.get("id") == bt.NUDGE_ACTION_ID for a in fresh.get("actions") or [])


# ── do_action / ask event routing (no direct model call) ──────────────────

def test_do_action_hands_off_via_instruction_event_only(tmp_path, monkeypatch):
    monkeypatch.setattr(bw, "run_delegation", _refusing_delegation)
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    card = _add_card(tmp_path, cluster="home")  # first default action: call_ahead
    _pace(tmp_path, card["id"], cadence="now", next_touch=_past(now), touch_action="do_action")

    results = bt.sweep(tmp_path, _network(), now=now)

    assert results[0]["result"] == "instructed"
    fresh = _fresh(tmp_path, card["id"])
    assert fresh["instructed"]["action_id"] == "call_ahead"
    events = _events(tmp_path)
    assert any(e["event"] == "instruction" for e in events)


# ── the miss sweep ──────────────────────────────────────────────────────

def test_miss_ledgered_when_nothing_records_a_touch_and_clears_on_next_fire(tmp_path, monkeypatch):
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    card = _add_card(tmp_path)
    _pace(tmp_path, card["id"], cadence="now", next_touch=_past(now), touch_action="remind")

    # First tick: delivery fails, so no record_touch happens for this touch_due.
    _stub_send(monkeypatch, ok=False, error="gateway down")
    bt.sweep(tmp_path, _network(), now=now)
    fresh = _fresh(tmp_path, card["id"])
    assert fresh.get("touches", []) == []  # no touch recorded — still due

    # Well past the window: the miss sweep ledgers it.
    later = now + timedelta(minutes=bt.DEFAULT_TOUCH_WINDOW_MINUTES + 5)
    bt._sweep_misses(tmp_path, now=later)  # noqa: SLF001
    day = later.strftime("%Y-%m-%d")
    ledger_path = bw.ledger_dir(tmp_path) / f"{day}.jsonl"
    rows = [json.loads(line) for line in ledger_path.read_text().splitlines() if line.strip()]
    misses = [r for r in rows if r["outcome"] == bw.OUTCOME_MISSED
              and r["action_id"] == f"{bt.TOUCH_MISS_PREFIX}remind"]
    assert len(misses) == 1
    fresh = _fresh(tmp_path, card["id"])
    assert fresh.get(bt.MISSED_STAMPS_FIELD)

    # A repeat miss-sweep pass must not double-ledger the same touch_due.
    bt._sweep_misses(tmp_path, now=later + timedelta(minutes=5))  # noqa: SLF001
    rows = [json.loads(line) for line in ledger_path.read_text().splitlines() if line.strip()]
    misses = [r for r in rows if r["outcome"] == bw.OUTCOME_MISSED
              and r["action_id"] == f"{bt.TOUCH_MISS_PREFIX}remind"]
    assert len(misses) == 1

    # The next successful fire clears the stamp (item 6: "clears on the
    # next fired touch").
    _stub_send(monkeypatch, ok=True)
    board = bs.load_board(tmp_path, BOT)
    bs.find_card(board, card["id"])["next_touch"] = _past(later)
    bs.save_board(tmp_path, BOT, board)
    bt.sweep(tmp_path, _network(), now=later + timedelta(minutes=6))
    fresh = _fresh(tmp_path, card["id"])
    assert bt.MISSED_STAMPS_FIELD not in fresh
    assert bt.LEGACY_MISSED_STAMP_FIELD not in fresh


def test_miss_sweep_uses_the_configured_worker_window(tmp_path):
    """The fix for the ``_window_for(action, {})`` bug: the miss sweep must
    honor the SAME operator-configured `window_min` the idempotency guard
    in `sweep()` already does, not a hardcoded-empty-dict default."""
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    card = _add_card(tmp_path, cluster="work")
    _pace(tmp_path, card["id"], cadence="soon", next_touch=_past(now), touch_action="check_source")
    network = _network()
    network["pod"] = {"board_worker": {"window_min": 180}}

    def boom(card, network):  # dispatch raises -> touch_due fires, nothing ever records it
        raise RuntimeError("probe unavailable")
    bt.sweep(tmp_path, network, now=now, probes={"manual": boom})
    fresh = _fresh(tmp_path, card["id"])
    assert fresh.get("touches", []) == []

    # 90 minutes later: inside the configured 180-minute window — not a miss yet.
    misses = bt._sweep_misses(tmp_path, network, now=now + timedelta(minutes=90))  # noqa: SLF001
    assert misses == []

    # past the configured window: now it's a miss.
    misses = bt._sweep_misses(tmp_path, network, now=now + timedelta(minutes=200))  # noqa: SLF001
    assert len(misses) == 1


def test_miss_sweep_catches_a_touch_due_older_than_a_day(tmp_path):
    """The fix for the hardcoded "today + yesterday" lookback: a daemon
    outage (box asleep over a weekend) must not let a stale ``touch_due``
    age out of the miss sweep's scan range before it's ever examined."""
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    card = _add_card(tmp_path)
    old_due = now - timedelta(days=3)
    bs.append_event(tmp_path, BOT, {
        "event": bt.TOUCH_DUE_EVENT, "card": card["id"], "title": card["title"],
        "action": "remind", "scheduled_for": bs._utcnow(old_due),  # noqa: SLF001
    }, now=old_due)

    misses = bt._sweep_misses(tmp_path, now=now)  # noqa: SLF001

    assert len(misses) == 1
    assert misses[0]["action_id"] == f"{bt.TOUCH_MISS_PREFIX}remind"
    assert misses[0]["producer"] == bt.TOUCH_PRODUCER


# ── weekly report ────────────────────────────────────────────────────────

def test_weekly_report_stats_and_line_on_a_fixture(tmp_path):
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    card = _add_card(tmp_path)
    card_id = card["id"]
    # Two touches fired this week; one of them is later ledgered a miss.
    for i in range(2):
        bs.append_event(tmp_path, BOT, {
            "event": bt.TOUCH_DUE_EVENT, "card": card_id, "action": "remind",
            "scheduled_for": _past(now),
        }, now=now - timedelta(days=i))
    bw.record_delivery_outcome(
        tmp_path, bot_id=BOT, card_id=card_id, action_id=f"{bt.TOUCH_MISS_PREFIX}remind",
        window_start=_past(now), window_end=_past(now, minutes=0),
        outcome=bw.OUTCOME_MISSED, now=now, producer=bt.TOUCH_PRODUCER)

    stats = bt.weekly_report_stats(tmp_path, BOT, now=now)
    assert stats["touches_due"] == 2
    assert stats["touches_missed"] == 1
    assert stats["touches_on_time"] == 1
    assert stats["captures_proposed"] == "unknown"

    line = bt.format_report_line(BOT, stats)
    assert line == (
        f"{BOT}: touches due 2 / on time 1 / missed 1; "
        "captures proposed unknown / kept unknown / dropped unknown"
    )


def test_weekly_report_stats_unknown_when_store_unreadable(tmp_path, monkeypatch):
    # A real, non-empty events file so the read path actually executes —
    # an absent file just contributes zero due touches, not "unknown".
    events_dir = bs.board_dir(tmp_path, BOT) / "events"
    events_dir.mkdir(parents=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (events_dir / f"{day}.jsonl").write_text('{"event": "touch_due"}\n')

    def boom(*a, **kw):
        raise OSError("permission denied")
    monkeypatch.setattr(Path, "read_text", boom)
    stats = bt.weekly_report_stats(tmp_path, BOT)
    assert stats == {"bot_id": BOT, "unknown": True}


def _events(tmp_path, card_id=None) -> list[dict]:
    d = bs.board_dir(tmp_path, BOT) / "events"
    out = []
    for p in sorted(d.glob("*.jsonl")):
        out.extend(json.loads(line) for line in p.read_text().splitlines() if line.strip())
    return [e for e in out if card_id is None or e.get("card") == card_id]


# ── reviews/pr-4410.md Hold 1 ───────────────────────────────────────────────


def _miss_rows(tmp_path, day: str, action: str = "remind") -> list[dict]:
    path = bw.ledger_dir(tmp_path) / f"{day}.jsonl"
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r["outcome"] == bw.OUTCOME_MISSED
            and r["action_id"] == f"{bt.TOUCH_MISS_PREFIX}{action}"]


def test_two_unresolved_touch_dues_ledger_once_each_and_never_again(tmp_path, monkeypatch):
    """Hold 1: the miss sweep scans a SEVEN-DAY window of touch_due events, so a
    card can hold several unresolved ones. The single-valued stamp could only
    remember the newest, and re-ledgered every older one on every tick.

    Two failed fires, two genuine misses, then two more sweeps that must add
    nothing. Against the pre-fix module the final assert sees four rows and
    keeps growing.
    """
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    card = _add_card(tmp_path)
    _stub_send(monkeypatch, ok=False, error="gateway down")

    # Fire 1 — delivery fails, so nothing records a touch.
    _pace(tmp_path, card["id"], cadence="now", next_touch=_past(now), touch_action="remind")
    bt.sweep(tmp_path, _network(), now=now)

    # Fire 2 — a full window later, still failing: a SECOND unresolved touch_due.
    second = now + timedelta(minutes=bt.DEFAULT_TOUCH_WINDOW_MINUTES + 1)
    board = bs.load_board(tmp_path, BOT)
    bs.find_card(board, card["id"])["next_touch"] = _past(second)
    bs.save_board(tmp_path, BOT, board)
    bt.sweep(tmp_path, _network(), now=second)

    # Both windows elapsed: two distinct misses, ledgered once each.
    later = second + timedelta(minutes=bt.DEFAULT_TOUCH_WINDOW_MINUTES + 5)
    bt._sweep_misses(tmp_path, now=later)  # noqa: SLF001
    day = later.strftime("%Y-%m-%d")
    assert len(_miss_rows(tmp_path, day)) == 2

    # Two further passes, the card still unresolved — which is exactly the
    # chronically-missed case this metric exists to measure.
    bt._sweep_misses(tmp_path, now=later + timedelta(minutes=5))  # noqa: SLF001
    bt._sweep_misses(tmp_path, now=later + timedelta(minutes=10))  # noqa: SLF001
    assert len(_miss_rows(tmp_path, day)) == 2


def test_a_legacy_scalar_stamp_still_suppresses_its_own_miss(tmp_path, monkeypatch):
    """Boards written by the previous cut carry ``touch_missed_at``. Reading it
    is what stops this change from re-ledgering every already-counted miss on
    the pod the first time the new code runs."""
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    card = _add_card(tmp_path)
    _stub_send(monkeypatch, ok=False, error="gateway down")
    _pace(tmp_path, card["id"], cadence="now", next_touch=_past(now), touch_action="remind")
    bt.sweep(tmp_path, _network(), now=now)

    due_ts = [e for e in _events(tmp_path, card["id"])
              if e.get("event") == bt.TOUCH_DUE_EVENT][0]["ts"]
    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    fresh.pop(bt.MISSED_STAMPS_FIELD, None)
    fresh[bt.LEGACY_MISSED_STAMP_FIELD] = due_ts   # as the old code wrote it
    bs.save_board(tmp_path, BOT, board)

    later = now + timedelta(minutes=bt.DEFAULT_TOUCH_WINDOW_MINUTES + 5)
    bt._sweep_misses(tmp_path, now=later)  # noqa: SLF001
    assert _miss_rows(tmp_path, later.strftime("%Y-%m-%d")) == []


def test_miss_stamps_are_pruned_past_the_lookback_window(tmp_path):
    """The list is bounded: a stamp older than the sweep's own lookback can
    never suppress anything again, so it is dropped rather than accumulated."""
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    ancient = (now - timedelta(minutes=bt.MISS_SWEEP_LOOKBACK_MINUTES + 60)
               ).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    card = {bt.MISSED_STAMPS_FIELD: [ancient, recent]}
    bt._record_miss_stamp(card, recent, now=now)  # noqa: SLF001
    assert ancient not in card[bt.MISSED_STAMPS_FIELD]
    assert recent in card[bt.MISSED_STAMPS_FIELD]


def test_an_unparsable_stamp_is_kept_rather_than_reopening_its_miss(tmp_path):
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    card = {bt.MISSED_STAMPS_FIELD: ["not-a-timestamp"]}
    bt._record_miss_stamp(card, "2026-09-22T11:00:00Z", now=now)  # noqa: SLF001
    assert "not-a-timestamp" in card[bt.MISSED_STAMPS_FIELD]

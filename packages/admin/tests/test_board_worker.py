"""Tests for board_worker.py (D-BI4/D-BI5, the execution half).

Fixture pod throughout — no live pod, no real model call (D-BI4's own
guardrail: "the PR claims no live delegation"). The model client is always a
small fake that records what it was asked and returns a fixed reply.

WHAT THESE PIN:
  * Wake on ``instruction`` and on an ``offered`` hand-over with no
    instruction yet (§1).
  * A second event on a busy card queues rather than starting a second
    worker (§1's "at most one worker per card").
  * The isolated session's context is EXACTLY card + enrichment + action +
    that card's own history — no chat, no memory, no board.list (CE-3).
  * Every fixed decline reason fires deterministically, with no model call.
  * accept -> board.progress -> done, with the result on the card and the
    cost in the run file.
  * A budget trip stops mid-run with the D-CC ``blocked`` note and keeps
    partial work.
  * The act/approval gate: below ``act_with_approval``, produce + request;
    resume on grant; decline ends the delegation.
  * D-BI6: an email-sourced target still needs approval even at rung
    ``act_with_approval``.
  * A silent run past its delivery window ledgers ``missed`` — and nothing
    retries it.
  * The weekly-receipt line shape.
  * Every learning-loop event's shape and actor.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import board_actions  # noqa: E402
from evolve_admin import board_store as bs  # noqa: E402
from evolve_admin import board_worker as bw  # noqa: E402

BOT = "personal-bot"


def _network(*, google=True) -> dict:
    return {"bots": {BOT: {}}, "pod": {"board_worker": {"per_card_usd": 0.50, "window_min": 60}}}


def _fake_client(text="a draft", in_tokens=100, out_tokens=50):
    calls = []

    def client(*, prompt, context):
        calls.append({"prompt": prompt, "context": context})
        return bw.ModelReply(text=text, input_tokens=in_tokens, output_tokens=out_tokens)

    client.calls = calls
    return client


def _refusing_client(*, prompt, context):  # pragma: no cover — asserted never called
    raise AssertionError("model client must not be called for a deterministic decline")


def _add_bot_card(tmp_path, *, cluster="fitness", source="manual", **kw) -> dict:
    board = bs.load_board(tmp_path, BOT)
    card = bs.add_card(board, title="Dentist", cluster=cluster, source=source, **kw)
    bs.save_board(tmp_path, BOT, board)
    return card


def _instruct(tmp_path, card_id, action_id, *, label=None, est_cost=0.01):
    board = bs.load_board(tmp_path, BOT)
    card = bs.resolve_card(board, card_id)
    action = board_actions.find_action(card, action_id)
    return bs.instruct_card(
        tmp_path, BOT, card_id, action_id=action_id,
        action_label=label or (action or {}).get("label", action_id),
        est_cost=est_cost, actor="user")


def _stub_action_context(monkeypatch, *, google=True, llm_est_cost=0.01):
    """``board_actions.bot_action_context`` reads real pod state (a Google
    token file on disk, a pricing cache) that a bare fixture pod has none
    of, so it always comes back ``{"google": False, "llm_est_cost": None}``
    — fine for the decline-gate tests that want exactly that, wrong for any
    test that needs to get PAST the integration/budget gates to exercise
    what happens next. This stubs it to fixed, deterministic values."""
    monkeypatch.setattr(
        board_actions, "bot_action_context",
        lambda bot_id, network, shared_dir: {"google": google, "llm_est_cost": llm_est_cost})


def _events(tmp_path, card_id=None) -> list[dict]:
    rows = bw.read_card_events(tmp_path, BOT, card_id) if card_id else []
    if card_id:
        return rows
    out = []
    d = bs.board_dir(tmp_path, BOT) / "events"
    for p in sorted(d.glob("*.jsonl")):
        out.extend(json.loads(line) for line in p.read_text().splitlines() if line.strip())
    return out


# ── decline gate ────────────────────────────────────────────────────────

def test_decline_missing_integration_no_model_call(tmp_path):
    network = _network()
    card = _add_bot_card(tmp_path, cluster="fitness")  # find_a_time needs google
    _instruct(tmp_path, card["id"], "find_a_time")
    result = bw.run_delegation(
        tmp_path, BOT, network, card["id"], "find_a_time",
        model_client=_refusing_client)
    assert result == {"outcome": "declined", "reason": bw.REASON_MISSING_INTEGRATION}
    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    assert fresh["delegation"]["state"] == "blocked"
    assert fresh["delegation"]["progress_note"] == "missing integration"


def test_decline_not_yet_supported_for_calling_integration(tmp_path):
    network = _network()
    card = _add_bot_card(tmp_path, cluster="home")  # call_ahead -> calling, RUNG_UNBUILT
    _instruct(tmp_path, card["id"], "call_ahead")
    result = bw.run_delegation(
        tmp_path, BOT, network, card["id"], "call_ahead",
        model_client=_refusing_client)
    assert result == {"outcome": "declined", "reason": bw.REASON_NOT_YET_SUPPORTED}


def test_decline_not_yet_supported_for_unimplemented_tool_action(tmp_path):
    # find_directions IS implemented (TOOL_ACTIONS); every kind:llm action
    # gets the generic draft handler regardless of id (see
    # resolve_step_handler) — so only a kind:tool id with no bespoke code
    # proves the "not implemented" branch.
    network = _network()
    card = _add_bot_card(tmp_path, cluster="admin")
    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    fresh["actions"] = [{"id": "mystery_action", "label": "Do a mystery thing",
                          "kind": "tool", "_integration": None}]
    bs.save_board(tmp_path, BOT, board)
    _instruct(tmp_path, card["id"], "mystery_action")
    result = bw.run_delegation(
        tmp_path, BOT, network, card["id"], "mystery_action",
        model_client=_refusing_client)
    assert result == {"outcome": "declined", "reason": bw.REASON_NOT_YET_SUPPORTED}


def test_decline_budget_insufficient(tmp_path):
    network = {"bots": {BOT: {}}, "pod": {"board_worker": {"per_card_usd": 0.0, "window_min": 60}}}
    card = _add_bot_card(tmp_path, cluster="home")  # find_directions: kind tool, $0 cost
    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    fresh["actions"] = [{"id": "draft_reminder", "label": "Draft a reminder note",
                          "kind": "llm", "_integration": None}]
    bs.save_board(tmp_path, BOT, board)
    _instruct(tmp_path, card["id"], "draft_reminder")
    # llm_est_cost is None with no pricing cache in this fixture -> budget_insufficient
    result = bw.run_delegation(
        tmp_path, BOT, network, card["id"], "draft_reminder",
        model_client=_refusing_client)
    assert result == {"outcome": "declined", "reason": bw.REASON_BUDGET_INSUFFICIENT}


def test_decline_needs_confirmation_email_target_tool_kind_backstop(tmp_path):
    network = _network()
    card = _add_bot_card(tmp_path, cluster="work", source="email")
    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    # Synthetic: a real send action would never be kind:tool + requires_confirm
    # on an email card (D-BI6's vocabulary already prevents it) — this proves
    # the backstop fires if one ever slipped through.
    fresh["actions"] = [{"id": "send_reply", "label": "Send a reply", "kind": "tool",
                          "_integration": None, "requires_confirm": True}]
    bs.save_board(tmp_path, BOT, board)
    _instruct(tmp_path, card["id"], "send_reply")
    result = bw.run_delegation(
        tmp_path, BOT, network, card["id"], "send_reply",
        model_client=_refusing_client)
    assert result == {"outcome": "declined", "reason": bw.REASON_NEEDS_CONFIRMATION}


def test_decline_rung_too_low_synthetic_min_rung(tmp_path):
    network = _network()
    card = _add_bot_card(tmp_path, cluster="admin")
    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    fresh["actions"] = [{"id": "full_autonomy_thing", "label": "Do it fully autonomously",
                          "kind": "llm", "_integration": None,
                          "min_rung": bw.RUNG_AUTONOMOUS}]
    bs.save_board(tmp_path, BOT, board)
    _instruct(tmp_path, card["id"], "full_autonomy_thing")
    result = bw.run_delegation(
        tmp_path, BOT, network, card["id"], "full_autonomy_thing",
        model_client=_refusing_client)
    assert result == {"outcome": "declined", "reason": bw.REASON_RUNG_TOO_LOW}


def test_every_decline_reason_note_matches_brief_fixed_set(tmp_path):
    assert bw.DECLINE_NOTE == {
        bw.REASON_MISSING_INTEGRATION: "missing integration",
        bw.REASON_RUNG_TOO_LOW: "rung too low",
        bw.REASON_NOT_YET_SUPPORTED: "can't yet — tracking (D-PA6)",
        bw.REASON_BUDGET_INSUFFICIENT: "budget insufficient",
        bw.REASON_NEEDS_CONFIRMATION: "needs confirmation (email-sourced target)",
    }


# ── isolated context (CE-3) ─────────────────────────────────────────────

def test_worker_context_is_card_enrichment_action_history_only(tmp_path):
    board = bs.load_board(tmp_path, BOT)
    card = bs.add_card(
        board, title="Book scan", cluster="health", source="manual",
        enrichment={"location": {"value": {"text": "123 Main St"}, "source": "user"}})
    bs.save_board(tmp_path, BOT, board)
    bs.assign_card(tmp_path, BOT, card["id"], "bot", actor="user")
    bs.append_event(tmp_path, BOT, {"event": "note", "card": card["id"], "actor": "user"})

    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    action = {"id": "find_directions", "label": "Find directions", "kind": "tool"}
    ctx = bw.build_worker_context(tmp_path, BOT, fresh, action)

    assert set(ctx.keys()) == bw.CONTEXT_KEYS
    assert ctx["card"]["title"] == "Book scan"
    assert ctx["enrichment"]["location"]["value"]["text"] == "123 Main St"
    assert ctx["action"] == {"id": "find_directions", "label": "Find directions", "kind": "tool"}
    assert any(e.get("event") == "note" for e in ctx["history"])
    # Absence, not filtering: these keys are never populated in the first
    # place.
    for forbidden in ("chat", "memory", "board", "board_list", "other_cards"):
        assert forbidden not in ctx


def test_worker_context_never_touches_board_list(tmp_path, monkeypatch):
    board = bs.load_board(tmp_path, BOT)
    card = bs.add_card(board, title="X", cluster="admin", source="manual")
    bs.save_board(tmp_path, BOT, board)

    def _boom(*a, **k):  # pragma: no cover — should never be reached
        raise AssertionError("build_worker_context must never call visible_cards/board.list")

    monkeypatch.setattr(bs, "visible_cards", _boom)
    action = {"id": "find_directions", "label": "Find directions", "kind": "tool"}
    bw.build_worker_context(tmp_path, BOT, card, action)  # must not raise


# ── accept -> done ───────────────────────────────────────────────────────

def test_accept_progress_done_with_result_and_cost_in_run_file(tmp_path):
    network = _network()
    card = _add_bot_card(tmp_path, cluster="home")  # find_directions, kind: tool
    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    fresh["enrichment"] = {"location": {"value": {"text": "1600 Pennsylvania Ave"},
                                         "source": "user", "captured_at": "2026-01-01T00:00:00Z"}}
    bs.save_board(tmp_path, BOT, board)
    _instruct(tmp_path, card["id"], "find_directions")
    result = bw.run_delegation(
        tmp_path, BOT, network, card["id"], "find_directions",
        model_client=_refusing_client)  # zero-cost tool action: no model call needed
    assert result["outcome"] == "done"
    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    assert fresh["delegation"]["state"] == "done"
    assert "1600 Pennsylvania Ave" in fresh["delegation"]["result"]["text"]
    assert fresh["delegation"]["result"]["cost"] == 0.0

    run = json.loads(bw.run_file_path(tmp_path, BOT, card["id"]).read_text())
    assert run["outcome"] == bw.OUTCOME_ON_TIME
    assert run["cost_usd"] == 0.0
    assert run["action_id"] == "find_directions"

    events = _events(tmp_path)
    # "delegation" is board_store's own generic transition log (one per
    # set_delegation_progress call, accept AND done); "accepted" is this
    # module's own named learning event (§8) — the two coexist by design.
    assert [e["event"] for e in events if e["card"] == card["id"]] == [
        "instruction", "delegation", "accepted", "delegation"]
    assert all(e.get("actor") == BOT for e in events if e["event"] == "accepted")


# ── budget trip ──────────────────────────────────────────────────────────

def test_budget_trip_stops_at_step_n_keeps_partial_work(tmp_path, monkeypatch):
    _stub_action_context(monkeypatch)
    network = {"bots": {BOT: {}}, "pod": {"board_worker": {"per_card_usd": 0.05, "window_min": 60}}}
    card = _add_bot_card(tmp_path, cluster="admin")
    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    fresh["actions"] = [{"id": "multi_step_thing", "label": "Do a multi-step thing",
                          "kind": "llm", "_integration": None}]
    bs.save_board(tmp_path, BOT, board)
    _instruct(tmp_path, card["id"], "multi_step_thing")

    calls = {"n": 0}

    def handler(ctx, model_client):
        calls["n"] += 1
        return bw.StepResult(
            done=False, result_text=f"partial result after step {calls['n']}",
            cost_usd=0.02)

    result = bw.run_delegation(
        tmp_path, BOT, network, card["id"], "multi_step_thing",
        model_client=_refusing_client,
        step_handlers={"multi_step_thing": handler})

    assert result["outcome"] == "budget_tripped"
    assert calls["n"] == 3  # 0.02, 0.04 ok; 0.06 > 0.05 trips on step 3
    assert "budget $0.05 reached at step 3" in result["note"]

    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    assert fresh["delegation"]["state"] == "blocked"
    assert fresh["delegation"]["progress_note"] == "budget $0.05 reached at step 3"
    assert "partial result after step 3" in fresh["delegation"]["result"]["text"]

    events = _events(tmp_path, card["id"])
    tripped = [e for e in events if e["event"] == "budget_tripped"]
    assert len(tripped) == 1
    assert tripped[0]["step"] == 3
    assert tripped[0]["actor"] == BOT


# ── approval gate (§5) ───────────────────────────────────────────────────

def _act_card(tmp_path, *, source="manual", requires_confirm=False):
    board = bs.load_board(tmp_path, BOT)
    card = bs.add_card(board, title="Book the appointment", cluster="health", source=source)
    fresh = bs.find_card(board, card["id"])
    fresh["actions"] = [{"id": "book_it", "label": "Book the appointment", "kind": "llm",
                          "_integration": board_actions._INTEGRATION_GOOGLE, "act": "book",
                          "requires_confirm": requires_confirm}]
    bs.save_board(tmp_path, BOT, board)
    return card


def test_act_below_rung_produces_draft_and_requests_approval(tmp_path, monkeypatch):
    _stub_action_context(monkeypatch)
    network = _network()
    card = _act_card(tmp_path)
    _instruct(tmp_path, card["id"], "book_it")
    client = _fake_client(text="Proposed: book Tuesday 2pm")
    result = bw.run_delegation(
        tmp_path, BOT, network, card["id"], "book_it", model_client=client)
    assert result["outcome"] == "approval_requested"
    assert len(client.calls) == 1  # ONE bounded call, never an agentic loop

    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    assert fresh["pending_approval"]["act"] == "book"
    assert "Proposed: book Tuesday 2pm" in fresh["pending_approval"]["payload_summary"]
    assert fresh["delegation"]["state"] == "in_progress"

    events = _events(tmp_path, card["id"])
    req = [e for e in events if e["event"] == "approval_requested"]
    assert len(req) == 1 and req[0]["actor"] == BOT


def test_approval_granted_resumes_without_a_second_model_call(tmp_path, monkeypatch):
    _stub_action_context(monkeypatch)
    network = _network()
    card = _act_card(tmp_path)
    _instruct(tmp_path, card["id"], "book_it")
    client = _fake_client(text="Proposed: book Tuesday 2pm")
    bw.run_delegation(tmp_path, BOT, network, card["id"], "book_it", model_client=client)

    result = bw.resume_delegation(tmp_path, BOT, card["id"], granted=True)
    assert result["outcome"] == "done"
    assert len(client.calls) == 1  # resuming never re-runs the model

    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    assert fresh["delegation"]["state"] == "done"
    # Not the stale "awaiting approval" note left over from the request step
    # — set_delegation_progress carries a note forward unless overwritten.
    assert fresh["delegation"]["progress_note"] == "approved"
    assert "pending_approval" not in fresh
    assert "Proposed: book Tuesday 2pm" in fresh["delegation"]["result"]["text"]

    events = _events(tmp_path, card["id"])
    granted = [e for e in events if e["event"] == "approval_granted"]
    assert len(granted) == 1 and granted[0]["actor"] == "user"


def test_approval_declined_ends_the_delegation(tmp_path, monkeypatch):
    _stub_action_context(monkeypatch)
    network = _network()
    card = _act_card(tmp_path)
    _instruct(tmp_path, card["id"], "book_it")
    client = _fake_client()
    bw.run_delegation(tmp_path, BOT, network, card["id"], "book_it", model_client=client)

    result = bw.resume_delegation(tmp_path, BOT, card["id"], granted=False)
    assert result["outcome"] == "declined"

    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    assert fresh["delegation"]["state"] == "blocked"
    assert fresh["delegation"]["progress_note"] == "approval declined"
    assert "pending_approval" not in fresh

    events = _events(tmp_path, card["id"])
    declined = [e for e in events if e["event"] == "declined"]
    assert declined[-1]["reason"] == "approval_declined"
    assert declined[-1]["actor"] == BOT  # the WORKER ends its own delegation
    approval_declined = [e for e in events if e["event"] == "approval_declined"]
    assert approval_declined[0]["actor"] == "user"  # the USER made the call


def test_resume_with_no_pending_approval_is_a_noop(tmp_path):
    card = _add_bot_card(tmp_path)
    result = bw.resume_delegation(tmp_path, BOT, card["id"], granted=True)
    assert result["outcome"] == "skipped"


# ── D-BI6: email-sourced target needs approval even at rung act ────────

def test_email_sourced_act_needs_approval_even_at_autonomous_rung(tmp_path, monkeypatch):
    _stub_action_context(monkeypatch)
    network = {
        "bots": {BOT: {}},
        "pod": {"board_worker": {
            "per_card_usd": 0.50, "window_min": 60,
            "rungs": {"google": bw.RUNG_AUTONOMOUS},  # fully autonomous grant
        }},
    }
    card = _act_card(tmp_path, source="email", requires_confirm=True)
    _instruct(tmp_path, card["id"], "book_it")
    client = _fake_client(text="Proposed reply to the sender")
    result = bw.run_delegation(
        tmp_path, BOT, network, card["id"], "book_it", model_client=client)
    # Even though google is granted RUNG_AUTONOMOUS, an email-sourced target
    # still stops for approval (the exfil edge, D-BI6/deviation 5).
    assert result["outcome"] == "approval_requested"


@pytest.mark.parametrize("rung, needs_approval", [
    (bw.RUNG_DRAFT_ONLY, True),
    (bw.RUNG_ACT_WITH_APPROVAL, True),
    (bw.RUNG_AUTONOMOUS, False),
])
def test_act_needs_approval_at_every_rung(rung, needs_approval):
    # pr-4279 review finding 1: only the top rung, autonomous_within_rules,
    # may skip the gate — act_with_approval is the rung an operator grants
    # BECAUSE they want to approve each act, so it must still gate.
    card = {"source": "manual"}
    action = {"act": "book", "_integration": "google"}
    network = {"pod": {"board_worker": {"rungs": {"google": rung}}}}
    assert bw._act_needs_approval(card, action, network) is needs_approval


@pytest.mark.parametrize("rung", [bw.RUNG_DRAFT_ONLY, bw.RUNG_ACT_WITH_APPROVAL, bw.RUNG_AUTONOMOUS])
def test_email_sourced_act_needs_approval_at_every_rung(rung):
    # D-BI6 fold-in: the exfil edge holds no matter how permissive the grant.
    card = {"source": "email"}
    action = {"act": "book", "_integration": "google", "requires_confirm": True}
    network = {"pod": {"board_worker": {"rungs": {"google": rung}}}}
    assert bw._act_needs_approval(card, action, network) is True


def test_rung_gate_self_check_passes_on_the_live_code():
    # The import-time self-check board_worker_runner relies on to refuse to
    # start (exit 2) if this gate is ever inverted again.
    bw._self_check_rung_gate()  # must not raise


def test_email_sourced_card_never_reaches_a_send_action(tmp_path):
    # D-BI6 as already shipped by board_actions: email cards only ever get
    # kind:llm draft actions from the real vocabulary.
    board = bs.load_board(tmp_path, BOT)
    card = bs.add_card(board, title="Re: dinner?", cluster="social", source="email")
    assert all(a["kind"] == "llm" for a in card["actions"])


# ── delivery window / silence is a miss ─────────────────────────────────

def test_silent_run_past_window_ledgers_missed_and_never_retries(tmp_path, monkeypatch):
    _stub_action_context(monkeypatch)
    network = {"bots": {BOT: {}}, "pod": {"board_worker": {"per_card_usd": 0.50, "window_min": 30}}}
    card = _add_bot_card(tmp_path, cluster="admin")
    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    fresh["actions"] = [{"id": "hangs_forever", "label": "Hangs forever", "kind": "llm",
                          "_integration": None}]
    bs.save_board(tmp_path, BOT, board)
    _instruct(tmp_path, card["id"], "hangs_forever")

    def hanging_handler(ctx, model_client):  # the daemon process dies mid-call
        raise RuntimeError("simulated crash mid-step")

    started_at = datetime(2026, 9, 14, 9, 0, 0, tzinfo=timezone.utc)
    # Simulate the daemon dying mid-run: run_delegation writes the in-flight
    # run file (accepted + window_deadline) right after acceptance, then the
    # process dies inside the handler call before any terminal
    # board.progress is ever written — an uncaught exception, not a
    # BudgetExceeded, is the only thing that leaves the run file non-terminal
    # from a single synchronous call.
    with pytest.raises(RuntimeError):
        bw.run_delegation(
            tmp_path, BOT, network, card["id"], "hangs_forever",
            model_client=_refusing_client, now=started_at,
            step_handlers={"hangs_forever": hanging_handler})

    past_deadline = started_at + timedelta(minutes=45)
    rows = bw.sweep_delivery_windows(tmp_path, now=past_deadline)
    assert len(rows) == 1
    assert rows[0]["outcome"] == bw.OUTCOME_MISSED
    assert rows[0]["heal"] == "none"

    ledger_file = bw.ledger_dir(tmp_path) / f"{past_deadline.date().isoformat()}.jsonl"
    assert ledger_file.exists()

    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    assert fresh.get("delivery_missed") is True
    # Still "accepted" (or whatever non-terminal state it was in) — no
    # auto-retry silently re-ran it.
    assert fresh["delegation"]["state"] == "accepted"

    events = _events(tmp_path, card["id"])
    assert any(e["event"] == "missed" for e in events)

    # A second sweep at the same or later time must not double-ledger.
    rows_again = bw.sweep_delivery_windows(tmp_path, now=past_deadline + timedelta(minutes=5))
    assert rows_again == []


def test_run_within_window_never_ledgers_missed(tmp_path):
    network = _network()
    card = _add_bot_card(tmp_path, cluster="home")
    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    fresh["enrichment"] = {"location": {"value": {"text": "Home"}, "source": "user"}}
    bs.save_board(tmp_path, BOT, board)
    _instruct(tmp_path, card["id"], "find_directions")
    bw.run_delegation(tmp_path, BOT, network, card["id"], "find_directions",
                       model_client=_refusing_client)
    rows = bw.sweep_delivery_windows(tmp_path, now=datetime.now(timezone.utc) + timedelta(hours=2))
    assert rows == []


# ── daemon subscriber: wake + busy/queue ────────────────────────────────

def test_poll_once_wakes_on_instruction(tmp_path):
    network = _network()
    card = _add_bot_card(tmp_path, cluster="home")
    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    fresh["enrichment"] = {"location": {"value": {"text": "Somewhere"}, "source": "user"}}
    bs.save_board(tmp_path, BOT, board)
    _instruct(tmp_path, card["id"], "find_directions")

    results = bw.poll_once(tmp_path, network, model_client=_refusing_client)
    assert [r["outcome"] for r in results] == ["done"]

    # A second poll with no new events dispatches nothing.
    assert bw.poll_once(tmp_path, network, model_client=_refusing_client) == []


def test_poll_once_wakes_on_offered_with_no_instruction(tmp_path):
    network = _network()
    card = _add_bot_card(tmp_path, cluster="admin")
    bs.assign_card(tmp_path, BOT, card["id"], "bot", actor="user")

    results = bw.poll_once(tmp_path, network, model_client=_refusing_client)
    assert [r["outcome"] for r in results] == ["waiting"]

    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    assert fresh["delegation"]["state"] == "accepted"
    # pr-4279 review finding 3: "accepted" alone reads as "bot working" on
    # the phone — the note says it's actually waiting on the operator.
    assert fresh["delegation"]["progress_note"] == "waiting for you to pick an action"
    # Never invents an action — no run file, no cost.
    assert not bw.run_file_path(tmp_path, BOT, card["id"]).exists()


def test_second_event_on_busy_card_queues_not_a_second_worker(tmp_path, monkeypatch):
    network = _network()
    card = _add_bot_card(tmp_path, cluster="home")
    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    fresh["enrichment"] = {"location": {"value": {"text": "Somewhere"}, "source": "user"}}
    bs.save_board(tmp_path, BOT, board)
    _instruct(tmp_path, card["id"], "find_directions")

    dispatched = []
    real_dispatch = bw._dispatch_one

    def spy(shared_dir, network, bot_id, event, *, model_client):
        dispatched.append(event.get("event"))
        return real_dispatch(shared_dir, network, bot_id, event, model_client=model_client)

    monkeypatch.setattr(bw, "_dispatch_one", spy)
    bw._mark_busy(tmp_path, BOT, card["id"], {"ts": "x", "event": "instruction"})
    results = bw.poll_once(tmp_path, network, model_client=_refusing_client)
    assert results == []  # busy: queued, not dispatched
    assert dispatched == []
    assert bw.queue_path(tmp_path, BOT, card["id"]).exists()

    bw._clear_busy(tmp_path, BOT, card["id"])
    results = bw.poll_once(tmp_path, network, model_client=_refusing_client)
    assert [r["outcome"] for r in results] == ["done"]
    assert not bw.queue_path(tmp_path, BOT, card["id"]).exists()


# ── stale busy marker (a dead runner must not park a card forever) ─────────

def test_stale_busy_marker_older_than_window_is_cleared_and_dispatched(tmp_path):
    # pr-4279 review finding 2: a marker left by a runner that died mid-run
    # (SIGKILL, power loss) must not queue every later event forever.
    network = _network()  # window_min=60
    card = _add_bot_card(tmp_path, cluster="home")
    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    fresh["enrichment"] = {"location": {"value": {"text": "Somewhere"}, "source": "user"}}
    bs.save_board(tmp_path, BOT, board)
    _instruct(tmp_path, card["id"], "find_directions")

    now = datetime.now(timezone.utc)
    stale_started = now - timedelta(minutes=61)  # window_min + 1
    bw._mark_busy(
        tmp_path, BOT, card["id"],
        {"ts": stale_started.strftime("%Y-%m-%dT%H:%M:%SZ"), "event": "instruction"})

    results = bw.poll_once(tmp_path, network, model_client=_refusing_client, now=now)
    assert [r["outcome"] for r in results] == ["done"]  # the delegation ran
    assert not bw.busy_path(tmp_path, BOT, card["id"]).exists()

    events = _events(tmp_path, card["id"])
    stale = [e for e in events if e["event"] == "stale_busy"]
    assert len(stale) == 1 and stale[0]["actor"] == BOT


def test_fresh_busy_marker_still_queues(tmp_path):
    network = _network()  # window_min=60
    card = _add_bot_card(tmp_path, cluster="home")
    _instruct(tmp_path, card["id"], "find_directions")

    now = datetime.now(timezone.utc)
    fresh_started = now - timedelta(minutes=1)
    bw._mark_busy(
        tmp_path, BOT, card["id"],
        {"ts": fresh_started.strftime("%Y-%m-%dT%H:%M:%SZ"), "event": "instruction"})

    results = bw.poll_once(tmp_path, network, model_client=_refusing_client, now=now)
    assert results == []  # still busy: queued, not dispatched
    assert bw.queue_path(tmp_path, BOT, card["id"]).exists()
    assert bw.busy_path(tmp_path, BOT, card["id"]).exists()


# ── receipt line ─────────────────────────────────────────────────────────

def test_receipt_line_shape(tmp_path):
    card = _add_bot_card(tmp_path, cluster="fitness")  # find_a_time
    board = bs.load_board(tmp_path, BOT)
    fresh = bs.find_card(board, card["id"])
    fresh["title"] = "dentist"
    bs.save_board(tmp_path, BOT, board)
    bs.instruct_card(tmp_path, BOT, card["id"], action_id="find_a_time",
                      action_label="Find a time that works", est_cost=0.04, actor="user")
    bs.set_delegation_progress(tmp_path, BOT, card["id"], state="accepted", actor=BOT)
    bs.set_delegation_progress(
        tmp_path, BOT, card["id"], state="done", cost_to_date=0.04,
        result={"text": "Tuesday 2pm works", "cost": 0.04}, actor=BOT)

    since = datetime.now(timezone.utc) - timedelta(days=1)
    until = datetime.now(timezone.utc) + timedelta(days=1)
    lines = bw.receipt_lines(tmp_path, BOT, since=since, until=until)
    assert len(lines) == 1
    assert lines[0]["line"] == "Find a time that works (dentist) — $0.04"
    assert lines[0]["cost_usd"] == 0.04


def test_format_receipt_line_matches_brief_example():
    assert bw.format_receipt_line("Find a time that works", "dentist", 0.04) == (
        "Find a time that works (dentist) — $0.04")


# ── config defaults ──────────────────────────────────────────────────────

def test_worker_config_defaults_when_pod_unconfigured():
    cfg = bw.worker_config({"bots": {}})
    assert cfg == {"per_card_usd": bw.DEFAULT_PER_CARD_BUDGET_USD,
                    "window_min": bw.DEFAULT_WINDOW_MINUTES}


def test_worker_config_reads_operator_override():
    cfg = bw.worker_config({"pod": {"board_worker": {"per_card_usd": 1.25, "window_min": 15}}})
    assert cfg == {"per_card_usd": 1.25, "window_min": 15}

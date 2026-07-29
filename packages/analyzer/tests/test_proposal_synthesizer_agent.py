"""tests/test_proposal_synthesizer_agent.py — Phase 4 tool-using agent loop.

Tests inject a deterministic ``llm_call`` that returns canned response
dicts so we can exercise:

  - the tool_use → tool_result conversation flow
  - parsing the final JSON output
  - budget enforcement (soft warning + hard cap)
  - failure modes (LLM exception, parse error, unknown stop_reason)
  - end-to-end routing (Proposal + Watchlist + SignalGap from one batch)

The fake ``llm_call`` is a stateful callable that returns scripted
responses in order. Each turn records the messages it was called with
so tests can assert on prompt construction.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from arbiter import store as proposal_store  # noqa: E402
from proposal_synthesizer import (  # noqa: E402
    store as candidate_store,
    synthesizer,
)
from proposal_synthesizer.budget import BudgetLimits  # noqa: E402
from schema.candidate_proposal import (  # noqa: E402
    CandidateProposal,
    Magnitude,
    new_candidate_id,
)
from schema.proposal import Investigation, RiskTag  # noqa: E402
from schema.provenance import Provenance  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────


def _candidate(
    *,
    bot_id: str = "admin_bot",
    variant: str = "heartbeat_no_model_override",
    aggregation: str = "substrate",
) -> CandidateProposal:
    return CandidateProposal(
        id=new_candidate_id(),
        bot_id=bot_id,
        state="synthesizing",
        generator_id="efficiency_hawk",
        dimension="efficiency",
        variant=variant,
        trigger_observations=[f"{variant}:{bot_id}"],
        provenance=Provenance(
            technique=f"efficiency_hawk.{variant}",
            signals={"heartbeat_every": "1h"},
            confidence=0.85,
        ),
        motivating_signals=[f"sig-{bot_id}-1"],
        magnitude=Magnitude(unit="sessions/week", value=168.0),
        draft_problem=f"{bot_id}: heartbeat runs on primary model every 1h",
        draft_headline=f"Route {bot_id} heartbeat to Haiku",
        draft_action=None,
        draft_risk_tag=None,
        draft_urgency="hygiene",
        draft_approval_audience="pod_operator",
        confidence=0.85,
        aggregation=aggregation,
    )


class ScriptedLLM:
    """A scripted tool-using LLM. ``responses`` is a list of dicts the
    fake will return in order. Each call records the system/messages/
    tools it was called with so tests can assert on shape.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, system: str, messages: list, tools: list) -> dict:
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        if not self._responses:
            raise AssertionError("ScriptedLLM exhausted; test expected more turns")
        return self._responses.pop(0)


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _tool_use_block(tool_name: str, args: dict, *, id_: str = "tu_1") -> dict:
    return {"type": "tool_use", "id": id_, "name": tool_name, "input": args}


def _response(*, content, stop_reason: str, input_tokens: int = 500, output_tokens: int = 100) -> dict:
    return {
        "content": content,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


# ── Single-turn (no tool use) ────────────────────────────────────────────────


def test_agent_single_turn_emits_proposal(tmp_path):
    c = _candidate()
    candidate_store.write_candidate(c, tmp_path)

    final_json = json.dumps({
        "outputs": [
            {
                "kind": "proposal",
                "motivating_candidates": [c.id],
                "bot_id": "<pod>",
                "headline": "Default new bots' heartbeat to Haiku",
                "problem": "3 bots run heartbeat on primary model",
                "action_kind": "Investigation",
                "action_context": "Update default in evolve deploy template.",
                "urgency": "improvement",
                "approval_audience": "pod_operator",
                "rationale": "Substrate-wide fix.",
            }
        ]
    })

    llm = ScriptedLLM([
        _response(content=[_text_block(final_json)], stop_reason="end_turn"),
    ])

    stats = synthesizer.synthesize_pending_with_tools(tmp_path, llm_call=llm)
    assert stats.proposals_emitted == 1
    assert stats.errors == []
    assert len(llm.calls) == 1
    # Tools were passed to the model.
    assert len(llm.calls[0]["tools"]) == 10

    proposals = list(proposal_store.iter_proposals(tmp_path, subdirs=("pending",)))
    assert len(proposals) == 1


# ── Multi-turn with tool use ─────────────────────────────────────────────────


def test_agent_calls_tool_then_emits(tmp_path, monkeypatch):
    """Two-turn flow: model calls read_cost_ledger, then emits a Proposal."""
    c = _candidate()
    candidate_store.write_candidate(c, tmp_path)

    # Mock the cost_ledger so the read_cost_ledger tool returns predictable data.
    import cost_ledger

    monkeypatch.setattr(
        cost_ledger,
        "read_events",
        lambda bot_id, days=7, shared_dir=None: iter([
            {"trigger_kind": "heartbeat", "cost_usd": 0.50, "ts": "2026-05-11T10:00:00Z"},
        ]),
    )

    final_json = json.dumps({
        "outputs": [
            {
                "kind": "proposal",
                "motivating_candidates": [c.id],
                "bot_id": "admin_bot",
                "headline": "Route admin_bot heartbeat to Haiku — $3.50/wk on Sonnet",
                "problem": "admin_bot: heartbeat runs on primary",
                "action_kind": "Investigation",
                "action_context": "Set heartbeat.model in openclaw.json",
                "urgency": "hygiene",
                "approval_audience": "pod_operator",
                "rationale": "Investigation showed real spend on heartbeats.",
            }
        ]
    })

    llm = ScriptedLLM([
        # Turn 1: tool_use
        _response(
            content=[_tool_use_block("read_cost_ledger", {"bot_id": "admin_bot", "days": 7})],
            stop_reason="tool_use",
        ),
        # Turn 2: final JSON
        _response(content=[_text_block(final_json)], stop_reason="end_turn"),
    ])

    stats = synthesizer.synthesize_pending_with_tools(tmp_path, llm_call=llm)
    assert stats.proposals_emitted == 1
    assert stats.errors == []
    assert len(llm.calls) == 2

    # Turn 2's messages should include the tool result.
    turn2_messages = llm.calls[1]["messages"]
    assert any(
        isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_result" for b in m["content"])
        for m in turn2_messages
    )


def test_agent_handles_multiple_tool_uses_in_one_turn(tmp_path, monkeypatch):
    """A single assistant turn with two tool_use blocks → two tool_result blocks."""
    c = _candidate()
    candidate_store.write_candidate(c, tmp_path)

    import cost_ledger
    monkeypatch.setattr(
        cost_ledger,
        "read_events",
        lambda bot_id, days=7, shared_dir=None: iter([]),
    )

    llm = ScriptedLLM([
        _response(
            content=[
                _tool_use_block("read_cost_ledger", {"bot_id": "admin_bot"}, id_="tu_a"),
                _tool_use_block("read_audit_findings", {}, id_="tu_b"),
            ],
            stop_reason="tool_use",
        ),
        _response(
            content=[_text_block(json.dumps({"outputs": []}))],
            stop_reason="end_turn",
        ),
    ])

    stats = synthesizer.synthesize_pending_with_tools(tmp_path, llm_call=llm)
    assert stats.errors == []

    # The user turn appended after Turn 1 should carry both tool_result blocks.
    turn2_messages = llm.calls[1]["messages"]
    last_user_msg = next(
        m for m in reversed(turn2_messages) if m.get("role") == "user"
    )
    tool_results = [
        b for b in last_user_msg["content"] if b.get("type") == "tool_result"
    ]
    assert len(tool_results) == 2
    assert {tr["tool_use_id"] for tr in tool_results} == {"tu_a", "tu_b"}


# ── Budget enforcement ───────────────────────────────────────────────────────


def test_agent_injects_soft_warning_then_continues(tmp_path):
    """Crossing the soft cap injects a wrap-up nudge but the model can still
    finish on its own terms."""
    c = _candidate()
    candidate_store.write_candidate(c, tmp_path)

    # Very tight soft cap so turn 1's usage trips it.
    limits = BudgetLimits(
        soft_cost_usd_per_candidate=0.0001,
        soft_turns_per_candidate=1,
        soft_cost_usd_per_run=0.0001,
        hard_cost_usd_per_candidate=100.0,
        hard_turns_per_candidate=100,
        hard_cost_usd_per_run=100.0,
    )

    llm = ScriptedLLM([
        _response(content=[_tool_use_block("read_audit_findings", {})], stop_reason="tool_use", input_tokens=10000, output_tokens=200),
        _response(content=[_text_block(json.dumps({"outputs": []}))], stop_reason="end_turn"),
    ])

    stats = synthesizer.synthesize_pending_with_tools(
        tmp_path, llm_call=llm, limits=limits
    )
    # Turn 2 should have received a soft-warning user message.
    turn2_messages = llm.calls[1]["messages"]
    soft_warn = [
        m for m in turn2_messages
        if isinstance(m.get("content"), str) and "past the soft" in m["content"]
    ]
    assert len(soft_warn) == 1


def test_agent_hard_cap_forces_final_turn(tmp_path):
    """Hard cap reached → agent injects BUDGET STOP and the next turn's
    output is treated as final regardless of stop_reason."""
    c = _candidate()
    candidate_store.write_candidate(c, tmp_path)

    limits = BudgetLimits(
        soft_cost_usd_per_candidate=0.0001,
        soft_turns_per_candidate=1,
        soft_cost_usd_per_run=0.0001,
        hard_cost_usd_per_candidate=0.0002,  # tiny; turn 1 trips it
        hard_turns_per_candidate=100,
        hard_cost_usd_per_run=100.0,
    )

    final_json = json.dumps({
        "outputs": [
            {
                "kind": "watchlist",
                "motivating_candidates": [c.id],
                "synthesizer_note": "Budget exceeded; emitting best-effort.",
            }
        ]
    })

    llm = ScriptedLLM([
        # Turn 1 (will trip the hard cap via input_tokens).
        _response(content=[_tool_use_block("read_audit_findings", {})], stop_reason="tool_use", input_tokens=500_000, output_tokens=200),
        # Turn 2: agent injected BUDGET STOP; model emits final JSON.
        _response(content=[_text_block(final_json)], stop_reason="end_turn"),
    ])

    stats = synthesizer.synthesize_pending_with_tools(
        tmp_path, llm_call=llm, limits=limits
    )
    assert stats.watchlist_entries == 1
    # The agent should have injected a "BUDGET STOP" message before turn 2.
    turn2_messages = llm.calls[1]["messages"]
    budget_stop = [
        m for m in turn2_messages
        if isinstance(m.get("content"), str) and "BUDGET STOP" in m["content"]
    ]
    assert len(budget_stop) == 1


# ── Failure modes ────────────────────────────────────────────────────────────


def test_agent_llm_exception_does_not_consume_candidate(tmp_path):
    c = _candidate()
    candidate_store.write_candidate(c, tmp_path)

    def boom(_sys, _msgs, _tools):
        raise RuntimeError("api down")

    stats = synthesizer.synthesize_pending_with_tools(tmp_path, llm_call=boom)
    assert any("llm_call_failed" in e for e in stats.errors)
    # Candidate remains for the next run.
    syn = list(candidate_store.iter_candidates(tmp_path, subdirs=("synthesizing",)))
    assert len(syn) == 1


def test_agent_parse_failure_records_error(tmp_path):
    c = _candidate()
    candidate_store.write_candidate(c, tmp_path)

    llm = ScriptedLLM([
        _response(content=[_text_block("not json")], stop_reason="end_turn"),
    ])
    stats = synthesizer.synthesize_pending_with_tools(tmp_path, llm_call=llm)
    assert any("json_parse_failure" in e for e in stats.errors)


def test_agent_tool_use_with_no_blocks_is_treated_as_end(tmp_path):
    """If the model says stop_reason=tool_use but emits no tool_use blocks,
    treat it as end_turn (don't loop forever)."""
    c = _candidate()
    candidate_store.write_candidate(c, tmp_path)

    llm = ScriptedLLM([
        _response(content=[_text_block('{"outputs": []}')], stop_reason="tool_use"),
    ])
    stats = synthesizer.synthesize_pending_with_tools(tmp_path, llm_call=llm)
    # Should not infinite-loop; ends cleanly.
    assert stats.errors == []


# ── Synthesis log includes budget snapshot ───────────────────────────────────


def test_synthesis_log_records_budget_in_phase4(tmp_path):
    c = _candidate()
    candidate_store.write_candidate(c, tmp_path)

    llm = ScriptedLLM([
        _response(
            content=[_text_block(json.dumps({
                "outputs": [
                    {
                        "kind": "watchlist",
                        "motivating_candidates": [c.id],
                        "synthesizer_note": "Watching.",
                    }
                ]
            }))],
            stop_reason="end_turn",
            input_tokens=1234,
            output_tokens=567,
        ),
    ])

    synthesizer.synthesize_pending_with_tools(tmp_path, llm_call=llm)

    log_path = candidate_store.synthesis_log_path(tmp_path)
    rec = json.loads(log_path.read_text().splitlines()[0])
    assert "budget" in rec
    assert rec["budget"]["run"]["input_tokens"] == 1234
    assert rec["budget"]["run"]["output_tokens"] == 567
    assert rec["budget"]["run"]["cost_usd"] > 0


# ── Mixed-batch end-to-end ───────────────────────────────────────────────────


def test_agent_mixed_batch_routes_three_output_kinds(tmp_path, monkeypatch):
    c1 = _candidate(bot_id="admin_bot", variant="cron_overactive", aggregation="none")
    c2 = _candidate(bot_id="team_bot_c", variant="heartbeat_no_model_override", aggregation="none")
    c3 = _candidate(bot_id="team_bot_b", variant="session_token_outlier", aggregation="none")
    for c in (c1, c2, c3):
        candidate_store.write_candidate(c, tmp_path)

    final = json.dumps({
        "outputs": [
            {
                "kind": "proposal",
                "motivating_candidates": [c1.id],
                "bot_id": "admin_bot",
                "headline": "Investigate admin_bot cron",
                "problem": "admin_bot cron over-fires",
                "action_kind": "Investigation",
                "action_context": "Check cron registry.",
                "urgency": "operational_urgent",
                "approval_audience": "pod_operator",
            },
            {
                "kind": "watchlist",
                "motivating_candidates": [c2.id],
                "synthesizer_note": "Small magnitude; watching.",
            },
            {
                "kind": "signal_gap",
                "motivating_candidates": [c3.id],
                "producer": "cost_watchdog",
                "signal_type": "session_tool_pattern",
                "description": "Need per-tool breakdown.",
                "suggested_data_shape": {"tools": "list"},
                "estimated_impact": "Would clarify outliers.",
            },
        ]
    })

    llm = ScriptedLLM([
        _response(content=[_text_block(final)], stop_reason="end_turn"),
    ])

    stats = synthesizer.synthesize_pending_with_tools(tmp_path, llm_call=llm)
    assert stats.proposals_emitted == 1
    assert stats.watchlist_entries == 1
    assert stats.signal_gaps_emitted == 1
    assert stats.errors == []

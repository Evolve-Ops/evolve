"""tests/test_proposal_synthesizer_llm.py

Coverage for the Phase 3 LLM synthesizer (no tool access yet, just
prose + structured output). Tests inject a deterministic fake
``llm_call`` to avoid network I/O.

Spec: docs/spec-proposal-synthesizer-2026-05-10.md §5, §6, Appendix A.
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
from schema.candidate_proposal import (  # noqa: E402
    CandidateProposal,
    Magnitude,
    new_candidate_id,
)
from schema.proposal import Investigation, RiskTag  # noqa: E402
from schema.provenance import Provenance  # noqa: E402


def _make_synthesizing(
    *,
    bot_id: str = "admin_bot",
    variant: str = "heartbeat_no_model_override",
    aggregation: str = "substrate",
    draft_action=None,
) -> CandidateProposal:
    """Build a candidate already in state='synthesizing' (typical input
    to the LLM synthesizer in Phase 3)."""
    c = CandidateProposal(
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
        draft_headline=f"Route {bot_id} heartbeat to Haiku — currently on primary",
        draft_action=draft_action,
        draft_risk_tag=(
            None
            if draft_action is None
            else RiskTag(blast_radius="bot", reversibility="manual", touches=[])
        ),
        draft_urgency="hygiene",
        draft_approval_audience="pod_operator",
        confidence=0.85,
        aggregation=aggregation,
    )
    return c


def _fake_llm(response_obj: dict):
    """Make a deterministic llm_call that returns the given dict as JSON."""
    payload = json.dumps(response_obj)

    def call(system_prompt: str, user_msg: str) -> str:
        # Sanity: charter should be in the system prompt, candidate
        # batch in the user message.
        assert "Substantiveness rubric" in system_prompt, "charter not loaded"
        assert "CandidateProposal" in user_msg
        return payload

    return call


# ── Output parsing ───────────────────────────────────────────────────────────


def test_parse_response_extracts_outputs_list():
    raw = '{"outputs": [{"kind": "drop", "motivating_candidates": ["x"]}]}'
    outputs, err = synthesizer._parse_response(raw)
    assert err == ""
    assert outputs == [{"kind": "drop", "motivating_candidates": ["x"]}]


def test_parse_response_strips_markdown_fence():
    raw = "```json\n{\"outputs\": []}\n```"
    outputs, err = synthesizer._parse_response(raw)
    assert err == ""
    assert outputs == []


def test_parse_response_rejects_non_object():
    outputs, err = synthesizer._parse_response("[1, 2, 3]")
    assert outputs is None
    assert "not a JSON object" in err


def test_parse_response_reports_json_error():
    outputs, err = synthesizer._parse_response("not json at all")
    assert outputs is None
    assert "json_parse_failure" in err


# ── Proposal-emit path ───────────────────────────────────────────────────────


def test_synthesizer_emits_proposal(tmp_path: Path):
    c = _make_synthesizing()
    candidate_store.write_candidate(c, tmp_path)

    llm = _fake_llm({
        "outputs": [
            {
                "kind": "proposal",
                "motivating_candidates": [c.id],
                "bot_id": "admin_bot",
                "headline": "Route admin_bot heartbeat to Haiku — currently on Sonnet",
                "problem": "admin_bot: heartbeat every 1h runs on primary model",
                "action_kind": "Investigation",
                "action_context": "Edit /Users/admin_bot/.openclaw/openclaw.json …",
                "urgency": "hygiene",
                "approval_audience": "pod_operator",
                "rationale": "168 sessions/week on Sonnet vs Haiku is meaningful.",
            }
        ]
    })

    stats = synthesizer.synthesize_pending(tmp_path, llm_call=llm)
    assert stats.proposals_emitted == 1
    assert stats.errors == []

    proposals = list(proposal_store.iter_proposals(tmp_path, subdirs=("pending",)))
    assert len(proposals) == 1
    p = proposals[0]
    assert p.admin_surface_summary.startswith("Route admin_bot heartbeat to Haiku")
    assert p.generator_id == "proposal_synthesizer"
    assert "synthesizer:heartbeat_no_model_override" in p.trigger_observations
    # The motivating signals trail back to the candidate's signals.
    assert "sig-admin_bot-1" in p.motivating_signals
    # Candidate is consumed from synthesizing/.
    assert list(candidate_store.iter_candidates(tmp_path, subdirs=("synthesizing",))) == []


def test_synthesizer_collapses_substrate_into_one_proposal(tmp_path: Path):
    """The signature Phase 3 case: a substrate aggregate (≥3 bots) yields
    one substrate-level Proposal."""
    c = _make_synthesizing(bot_id="<pod>", aggregation="substrate")
    c.aggregated_from = ["c-team_bot_c", "c-team_bot_b", "c-admin_bot"]
    candidate_store.write_candidate(c, tmp_path)

    llm = _fake_llm({
        "outputs": [
            {
                "kind": "proposal",
                "motivating_candidates": [c.id],
                "bot_id": "<pod>",
                "headline": "Default new bots' heartbeat to Haiku — affects 3 bots today",
                "problem": "3 bots run heartbeat on primary model; change the default in evolve.",
                "action_kind": "Investigation",
                "action_context": "Update default heartbeat config in evolve deploy template.",
                "urgency": "improvement",
                "approval_audience": "pod_operator",
                "rationale": "Substrate-wide; one fix avoids per-bot repetition.",
            }
        ]
    })

    stats = synthesizer.synthesize_pending(tmp_path, llm_call=llm)
    assert stats.proposals_emitted == 1
    proposals = list(proposal_store.iter_proposals(tmp_path, subdirs=("pending",)))
    assert proposals[0].bot_id == "<pod>"


# ── Watchlist path ───────────────────────────────────────────────────────────


def test_synthesizer_writes_watchlist_with_note(tmp_path: Path):
    c = _make_synthesizing(aggregation="none", draft_action=Investigation(context="vague"))
    candidate_store.write_candidate(c, tmp_path)

    llm = _fake_llm({
        "outputs": [
            {
                "kind": "watchlist",
                "motivating_candidates": [c.id],
                "synthesizer_note": "Magnitude is small; watch for more occurrences.",
            }
        ]
    })

    stats = synthesizer.synthesize_pending(tmp_path, llm_call=llm)
    assert stats.watchlist_entries == 1
    wl = list(candidate_store.iter_candidates(tmp_path, subdirs=("watchlist",)))
    assert len(wl) == 1
    assert wl[0].state == "watchlist"
    assert wl[0].synthesizer_note.startswith("Magnitude is small")
    # Candidate moved from synthesizing/ to watchlist/.
    assert list(candidate_store.iter_candidates(tmp_path, subdirs=("synthesizing",))) == []


# ── SignalGap path ───────────────────────────────────────────────────────────


def test_synthesizer_emits_signal_gap_proposal(tmp_path: Path):
    c = _make_synthesizing()
    candidate_store.write_candidate(c, tmp_path)

    llm = _fake_llm({
        "outputs": [
            {
                "kind": "signal_gap",
                "motivating_candidates": [c.id],
                "producer": "cost_watchdog",
                "signal_type": "heartbeat_tool_use_pattern",
                "description": "Need to know which tools heartbeats actually invoke.",
                "suggested_data_shape": {"tool_invocations": "list of tool_name"},
                "estimated_impact": "Would let me distinguish stuck heartbeats from useful ones.",
            }
        ]
    })

    stats = synthesizer.synthesize_pending(tmp_path, llm_call=llm)
    assert stats.signal_gaps_emitted == 1

    proposals = list(proposal_store.iter_proposals(tmp_path, subdirs=("pending",)))
    assert len(proposals) == 1
    p = proposals[0]
    assert p.action.kind == "AddSignalCollection"
    assert p.action.producer == "cost_watchdog"
    assert p.action.signal_type == "heartbeat_tool_use_pattern"
    assert p.bot_id == "<pod>"
    assert p.dimension == "observability"


# ── Drop path ────────────────────────────────────────────────────────────────


def test_synthesizer_drops_with_rationale(tmp_path: Path):
    c = _make_synthesizing()
    candidate_store.write_candidate(c, tmp_path)

    llm = _fake_llm({
        "outputs": [
            {
                "kind": "drop",
                "motivating_candidates": [c.id],
                "rationale": "Magnitude doesn't justify operator attention.",
            }
        ]
    })

    stats = synthesizer.synthesize_pending(tmp_path, llm_call=llm)
    assert stats.drops == 1
    # Drop record landed in the dropped log.
    log = candidate_store.dropped_log_path(tmp_path)
    assert log.exists()
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["reason"] == "synthesizer_dropped"
    assert "rationale" in rec.get("note", "") or rec["note"]


# ── Failure modes ────────────────────────────────────────────────────────────


def test_synthesizer_llm_failure_does_not_consume_candidates(tmp_path: Path):
    """An exception from llm_call leaves candidates in synthesizing/ for
    the next run."""
    c = _make_synthesizing()
    candidate_store.write_candidate(c, tmp_path)

    def boom(_sys, _msg):
        raise RuntimeError("API connection refused")

    stats = synthesizer.synthesize_pending(tmp_path, llm_call=boom)
    assert stats.errors and "llm_call_failed" in stats.errors[0]
    # Candidate remains for the next run.
    syn = list(candidate_store.iter_candidates(tmp_path, subdirs=("synthesizing",)))
    assert len(syn) == 1


def test_synthesizer_parse_failure_logs_error(tmp_path: Path):
    c = _make_synthesizing()
    candidate_store.write_candidate(c, tmp_path)

    def garbage(_sys, _msg):
        return "this is not JSON at all"

    stats = synthesizer.synthesize_pending(tmp_path, llm_call=garbage)
    assert stats.errors
    assert "json_parse_failure" in stats.errors[0]


def test_synthesizer_no_candidates_is_noop(tmp_path: Path):
    """Empty synthesizing/ — synthesizer returns empty stats without
    invoking the LLM."""
    calls = []

    def spy(sys_prompt, user_msg):
        calls.append((sys_prompt, user_msg))
        return '{"outputs": []}'

    stats = synthesizer.synthesize_pending(tmp_path, llm_call=spy)
    assert stats.candidates_read == 0
    assert stats.proposals_emitted == 0
    assert calls == []  # LLM never called when batch is empty


# ── Mixed batch ──────────────────────────────────────────────────────────────


def test_synthesizer_handles_mixed_output_batch(tmp_path: Path):
    """One LLM call → proposal + watchlist + signal_gap in one batch."""
    c1 = _make_synthesizing(bot_id="admin_bot", variant="cron_overactive", aggregation="none")
    c2 = _make_synthesizing(bot_id="team_bot_c", variant="heartbeat_no_model_override", aggregation="none")
    c3 = _make_synthesizing(bot_id="team_bot_b", variant="session_token_outlier", aggregation="none")
    for c in (c1, c2, c3):
        candidate_store.write_candidate(c, tmp_path)

    llm = _fake_llm({
        "outputs": [
            {
                "kind": "proposal",
                "motivating_candidates": [c1.id],
                "bot_id": "admin_bot",
                "headline": "Investigate admin_bot cron over-firing",
                "problem": "admin_bot: cron fired 41x in 24h",
                "action_kind": "Investigation",
                "action_context": "Check cron registration.",
                "urgency": "operational_urgent",
                "approval_audience": "pod_operator",
                "rationale": "Acute.",
            },
            {
                "kind": "watchlist",
                "motivating_candidates": [c2.id],
                "synthesizer_note": "Magnitude small; watching.",
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

    stats = synthesizer.synthesize_pending(tmp_path, llm_call=llm)
    assert stats.proposals_emitted == 1
    assert stats.watchlist_entries == 1
    assert stats.signal_gaps_emitted == 1
    assert stats.errors == []

    # All three candidates consumed from synthesizing/.
    assert list(candidate_store.iter_candidates(tmp_path, subdirs=("synthesizing",))) == []
    # Two Proposals (regular + signal_gap), one watchlist.
    proposals = list(proposal_store.iter_proposals(tmp_path, subdirs=("pending",)))
    assert len(proposals) == 2
    kinds = {p.action.kind for p in proposals}
    assert "Investigation" in kinds
    assert "AddSignalCollection" in kinds
    wl = list(candidate_store.iter_candidates(tmp_path, subdirs=("watchlist",)))
    assert len(wl) == 1


# ── Synthesis log ────────────────────────────────────────────────────────────


def test_synthesis_log_records_per_run(tmp_path: Path):
    c = _make_synthesizing()
    candidate_store.write_candidate(c, tmp_path)

    llm = _fake_llm({
        "outputs": [
            {
                "kind": "watchlist",
                "motivating_candidates": [c.id],
                "synthesizer_note": "Watching.",
            }
        ]
    })
    synthesizer.synthesize_pending(tmp_path, llm_call=llm)

    log_path = candidate_store.synthesis_log_path(tmp_path)
    assert log_path.exists()
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["candidate_ids"] == [c.id]
    assert rec["stats"]["watchlist_entries"] == 1

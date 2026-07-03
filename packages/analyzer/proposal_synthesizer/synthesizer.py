"""proposal_synthesizer.synthesizer — Phase 3 LLM-driven batch synthesis.

Spec: docs/spec-proposal-synthesizer-2026-05-10.md §5, §6, Appendix A.

The synthesizer reads everything in ``candidates/synthesizing/`` as a
single batch, asks an LLM (governed by the charter at
``proposal_synthesizer/charter.md``) what to do, and routes each
emitted output to the right store:

  - ``proposal``    → ``proposals/pending/<id>.json`` via arbiter.store
  - ``watchlist``   → ``candidates/watchlist/<id>.json`` (state=watching,
                      synthesizer_note populated)
  - ``signal_gap``  → ``proposals/pending/<id>.json`` with an
                      ``AddSignalCollection`` action (pod-targeted)
  - ``drop``        → record_drop with reason="synthesizer_dropped"

Phase 3 contract:
  - **No tool access.** The synthesizer only sees the candidates' draft
    fields. Investigation tools land in Phase 4.
  - **One LLM call per batch.** Batches all synthesizing/ candidates
    into a single prompt; the LLM's output is a JSON object with an
    ``outputs`` list whose items each name their motivating candidates.
  - **Cheap.** Haiku is the default model. Expected cost per run: well
    under a cent for typical batches (≤10 candidates).
  - **Fail-soft.** Any failure in LLM call / JSON parse / output write
    leaves the candidate where it was (synthesizing/) — the next run
    will retry.

Caller injects ``llm_call``; tests use a deterministic fake. The
default caller wires to Anthropic's API via the evolve bot's auth
profile per the spec's "evolve is the universal sysadmin partner"
rule.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from arbiter import store as proposal_store
from arbiter.ingest import _refresh_existing
from arbiter.track_record import bump_proposals_emitted
from proposal_synthesizer import store as candidate_store
from proposal_synthesizer.budget import Budget, BudgetLimits, DEFAULT_LIMITS
from proposal_synthesizer.promote import _build_proposal_from_candidate
from proposal_synthesizer.tools import (
    anthropic_tools_schema,
    dispatch_tool,
)
from schema.candidate_proposal import CandidateProposal
from schema.proposal import (
    AddSignalCollection,
    AgentsAppend,
    Claim,
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    TierAdjustment,
    WorkflowInstruction,
    new_proposal_id,
)


log = logging.getLogger(__name__)


# Broken-config fallbacks for Phase 3 (synthesis, no tools) and Phase 4
# (tool-using agent loop). Real selection routes through the pod's tier
# system via ``_resolve_pod_anthropic_tier`` so operator-configured
# tier3/tier2 swaps propagate here automatically. The literals only
# matter when the analyzer package isn't importable from its own
# subpackage (test isolation) or tier resolves to a non-Anthropic
# provider — the synthesizer calls the Anthropic SDK directly and
# can't dispatch openai/google models from here.
_DEFAULT_MODEL_FALLBACK = "claude-haiku-4-5"        # Phase 3 (synthesis)
DEFAULT_MAX_TOKENS = 4000

_DEFAULT_AGENT_MODEL_FALLBACK = "claude-sonnet-4-6"  # Phase 4 (tool-using)
DEFAULT_AGENT_MAX_TOKENS = 8000

# Sentinels that let the factory functions distinguish "caller wants the
# pod's tier default" from "caller passed an explicit model id". Without
# these, callers couldn't pin a specific id without disabling tier
# resolution outright — important for evaluation harnesses + tests.
_USE_TIER3_DEFAULT: object = object()
_USE_TIER2_DEFAULT: object = object()


def _resolve_pod_anthropic_tier(tier: str, fallback: str) -> str:
    """Resolve a pod-wide tier to a bare Anthropic model id.

    The proposal synthesizer is pod-wide work (evolve-bot's universal
    sysadmin role — see synthesizer spec §5.1), so no per-bot
    ``tier_assignments`` override is consulted; the pod's tier config
    is the source of truth.

    The Anthropic SDK call sites accept ``claude-sonnet-4-6`` form,
    not the ``anthropic/<model>`` form used by the tier registry, so
    we strip the prefix. Non-Anthropic tier resolutions (e.g. tier3
    falling back to ``openai/gpt-4o-mini``) yield the hardcoded
    fallback with a warning — the SDK can't dispatch those.

    Mirrors the resolver pattern used by ``arbiter.refine._resolve_refine_model``
    and ``applications.forge_engine._resolve_tier2_anthropic``.
    """
    try:
        from evolve_config import load_config  # type: ignore
        from models import resolve_tier  # type: ignore
        resolved = resolve_tier(tier, load_config())
    except Exception as exc:
        log.debug("proposal_synthesizer: %s resolve failed: %s", tier, exc)
        return fallback
    if not resolved.startswith("anthropic/"):
        log.warning(
            "proposal_synthesizer: %s resolved to non-Anthropic %r — "
            "synthesizer uses the Anthropic SDK directly and can't "
            "dispatch this. Falling back to %r.",
            tier, resolved, fallback,
        )
        return fallback
    return resolved.split("/", 1)[1]

# Absolute backup loop limit. The Budget enforces the real caps; this
# is just a guard against pathological "model keeps requesting one
# more tool" cases the budget hasn't caught yet.
ABSOLUTE_TURN_LIMIT = 50


CHARTER_PATH = Path(__file__).parent / "charter.md"


# ─────────────────────────────────────────────────────────────────────────────
# Stats
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SynthesizerStats:
    """Outcome of a single synthesizer run."""

    candidates_read: int = 0
    proposals_emitted: int = 0
    watchlist_entries: int = 0
    signal_gaps_emitted: int = 0
    drops: int = 0
    errors: list[str] = field(default_factory=list)
    raw_response: str = ""  # for debugging / synthesis_log


# ─────────────────────────────────────────────────────────────────────────────
# Prompt assembly
# ─────────────────────────────────────────────────────────────────────────────


def _load_charter() -> str:
    """Read the charter from disk. Cached at module load on first call."""
    return CHARTER_PATH.read_text(encoding="utf-8")


def _candidate_payload(c: CandidateProposal) -> dict:
    """Strip a candidate to the fields the synthesizer needs to reason.

    The LLM doesn't need the full provenance.signals payload (often
    large) or the schema_version metadata. Pass the parts that inform
    framing + decision.
    """
    return {
        "id": c.id,
        "bot_id": c.bot_id,
        "generator_id": c.generator_id,
        "variant": c.variant,
        "fingerprint": c.fingerprint,
        "aggregation": c.aggregation,
        "aggregated_from": list(c.aggregated_from),
        "motivating_signals": list(c.motivating_signals),
        "magnitude": (
            c.magnitude.to_dict() if c.magnitude else None
        ),
        "draft_problem": c.draft_problem,
        "draft_headline": c.draft_headline,
        "draft_action_kind": (
            getattr(c.draft_action, "kind", None) if c.draft_action else None
        ),
        "draft_action_context": (
            getattr(c.draft_action, "context", "")
            if c.draft_action is not None
            else ""
        ),
        "draft_urgency": c.draft_urgency,
        "confidence": c.confidence,
        "created_at": c.created_at,
    }


def _build_user_message(candidates: list[CandidateProposal]) -> str:
    """Assemble the user-side payload for the LLM call.

    The charter is the system prompt; this is just the batch data plus
    a short framing line so the model knows what to do with it.
    """
    payload = {
        "batch_size": len(candidates),
        "candidates": [_candidate_payload(c) for c in candidates],
    }
    return (
        "Process the following batch of CandidateProposals. Apply your "
        "charter (substantiveness rubric, framing rules, output "
        "contract) and return a single JSON object as specified in "
        "the charter's \"Response format\" section.\n\n"
        f"{json.dumps(payload, indent=2)}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Response parsing
# ─────────────────────────────────────────────────────────────────────────────


def _parse_response(raw: str) -> tuple[list[dict] | None, str]:
    """Extract the ``outputs`` list from the LLM's JSON response.

    Returns ``(outputs, error)``. On success ``outputs`` is the list
    and ``error`` is empty; on failure ``outputs`` is None and
    ``error`` is a one-line explanation.

    Tolerates leading/trailing whitespace and stray markdown fences
    (the charter says no fences, but models sometimes ignore that).
    """
    text = raw.strip()
    # Strip a leading ```json fence if present.
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl > 0:
            text = text[first_nl + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[: -3].rstrip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"json_parse_failure: {e}"
    if not isinstance(data, dict):
        return None, "response is not a JSON object"
    outputs = data.get("outputs")
    if outputs is None:
        return None, "response missing 'outputs' field"
    if not isinstance(outputs, list):
        return None, "'outputs' is not a list"
    return outputs, ""


# ─────────────────────────────────────────────────────────────────────────────
# Output writers
# ─────────────────────────────────────────────────────────────────────────────


def _action_from_output(out: dict) -> Any | None:
    """Build an Action variant from the LLM output's action_* fields."""
    kind = out.get("action_kind") or "Investigation"
    if kind == "Investigation":
        return Investigation(context=out.get("action_context", ""))
    if kind == "TierAdjustment":
        return TierAdjustment(
            bot_id=out.get("bot_id", "<unknown>"),
            target_class=out.get("action_target_class", "maintenance"),
            new_tier=out.get("action_new_tier", "haiku"),
        )
    if kind == "AgentsAppend":
        return AgentsAppend(
            bot_id=out.get("bot_id", "<unknown>"),
            section=out.get("action_section", "Streamline"),
            content=out.get("action_context", ""),
        )
    if kind == "WorkflowInstruction":
        return WorkflowInstruction(
            bot_id=out.get("bot_id", "<unknown>"),
            path=out.get("action_path", "workspace/note.md"),
            content=out.get("action_context", ""),
        )
    # Unknown kind — fall back to Investigation so we still produce
    # something the operator can see and revise.
    return Investigation(context=out.get("action_context", ""))


def _write_proposal_output(
    out: dict,
    candidate_lookup: dict[str, CandidateProposal],
    shared_dir: Path,
) -> tuple[bool, str]:
    """Build + write a Proposal from a synthesizer 'proposal' output."""
    motivating_ids = out.get("motivating_candidates") or []
    sample = next(
        (candidate_lookup[c] for c in motivating_ids if c in candidate_lookup),
        None,
    )
    if sample is None:
        return False, "no motivating candidate matched"

    motivating_signals: list[str] = []
    for cid in motivating_ids:
        if cid in candidate_lookup:
            motivating_signals.extend(candidate_lookup[cid].motivating_signals)
    motivating_signals = list(dict.fromkeys(motivating_signals))

    proposal = Proposal(
        id=new_proposal_id(),
        bot_id=out.get("bot_id") or sample.bot_id,
        generator_id="proposal_synthesizer",
        dimension=sample.dimension or "efficiency",
        trigger_observations=[
            f"synthesizer:{sample.variant}",
            *(f"motivated_by_candidate:{cid}" for cid in motivating_ids),
        ],
        provenance=Provenance(
            technique="proposal_synthesizer.batch_synthesis",
            signals={
                "rationale": out.get("rationale", ""),
                "candidate_ids": list(motivating_ids),
            },
            confidence=float(sample.confidence or 0.8),
        ),
        problem=out.get("problem", "") or sample.draft_problem,
        action=_action_from_output(out),
        risk_tag=sample.draft_risk_tag
        or RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        approval_audience=out.get("approval_audience") or "pod_operator",
        urgency=out.get("urgency") or "improvement",
        admin_surface_summary=(out.get("headline") or "")[:120],
        motivating_signals=motivating_signals,
        status="pending",
    )

    # Dedup against open Proposals — same path the mechanical promoter
    # uses, so we don't double-write when the synthesizer re-processes
    # a batch.
    # check-then-write under the store lock (7.1 Phase A) so a
    # concurrent emitter can't slip a duplicate in between.
    with proposal_store.locked(shared_dir):
        existing = proposal_store.find_open_duplicate(proposal, shared_dir)
        if existing is not None:
            _refresh_existing(existing, incoming=proposal, actor="proposal_synthesizer")
            located = proposal_store.find_proposal(shared_dir, existing.id)
            if located is not None:
                _, _, src_subdir = located
                proposal_store.move_proposal(existing, shared_dir, from_subdir=src_subdir)
            else:
                proposal_store.write_proposal(existing, shared_dir)
            _was_refresh = True
        else:
            proposal_store.write_proposal(proposal, shared_dir)
            _was_refresh = False
    if not _was_refresh:
        # The Proposal carries generator_id="proposal_synthesizer", which
        # isn't a registered coach. Credit the actual source coaches by
        # bumping each unique motivating candidate's generator_id —
        # otherwise Phase 6c coaches whose candidates the LLM fused into
        # a single Proposal would still appear inert in the Coaches table.
        for gen_id in {
            candidate_lookup[c].generator_id
            for c in motivating_ids
            if c in candidate_lookup
        }:
            bump_proposals_emitted(shared_dir, gen_id)
    return True, ""


def _write_signal_gap_output(
    out: dict,
    candidate_lookup: dict[str, CandidateProposal],
    shared_dir: Path,
) -> tuple[bool, str]:
    """Build + write a SignalGapProposal (a Proposal with AddSignalCollection)."""
    motivating_ids = list(out.get("motivating_candidates") or [])
    motivating_signals: list[str] = []
    for cid in motivating_ids:
        if cid in candidate_lookup:
            motivating_signals.extend(candidate_lookup[cid].motivating_signals)
    motivating_signals = list(dict.fromkeys(motivating_signals))

    producer = out.get("producer", "") or "unknown_producer"
    signal_type = out.get("signal_type", "") or "unknown_signal_type"
    description = out.get("description", "") or "(no description)"
    suggested_shape = out.get("suggested_data_shape") or {}
    estimated_impact = out.get("estimated_impact", "")

    headline = (
        f"Add signal '{signal_type}' to {producer} "
        f"— synthesizer needed it"
    )[:120]
    problem = (
        f"<pod>: synthesizer hit a signal gap. {description} "
        f"({len(motivating_ids)} candidate(s) would have benefited.)"
    )

    proposal = Proposal(
        id=new_proposal_id(),
        bot_id="<pod>",
        generator_id="proposal_synthesizer",
        dimension="observability",
        trigger_observations=[
            f"signal_gap:{producer}:{signal_type}",
        ],
        provenance=Provenance(
            technique="proposal_synthesizer.signal_gap",
            signals={
                "producer": producer,
                "signal_type": signal_type,
                "candidate_ids": motivating_ids,
            },
            confidence=0.7,
        ),
        problem=problem,
        action=AddSignalCollection(
            producer=producer,
            signal_type=signal_type,
            description=description,
            suggested_data_shape=dict(suggested_shape)
            if isinstance(suggested_shape, dict)
            else {},
            motivating_candidate_ids=motivating_ids,
            estimated_impact=estimated_impact,
        ),
        risk_tag=RiskTag(blast_radius="platform", reversibility="manual", touches=[]),
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary=headline,
        motivating_signals=motivating_signals,
        status="pending",
    )
    proposal_store.write_proposal(proposal, shared_dir)
    return True, ""


def _write_watchlist_output(
    out: dict,
    candidate_lookup: dict[str, CandidateProposal],
    shared_dir: Path,
) -> tuple[bool, str]:
    """Move each motivating candidate to watchlist/ with the
    synthesizer's note attached."""
    note = out.get("synthesizer_note") or ""
    motivating_ids = list(out.get("motivating_candidates") or [])
    moved = 0
    for cid in motivating_ids:
        cand = candidate_lookup.get(cid)
        if cand is None:
            continue
        cand.state = "watchlist"
        cand.synthesizer_note = note
        candidate_store.write_candidate(cand, shared_dir)
        candidate_store.delete_candidate(shared_dir, cid, subdir="synthesizing")
        moved += 1
    return moved > 0, "" if moved else "no candidates matched"


# ─────────────────────────────────────────────────────────────────────────────
# Top-level entry point
# ─────────────────────────────────────────────────────────────────────────────


def synthesize_batch(
    candidates: list[CandidateProposal],
    *,
    llm_call: Callable[[str, str], str],
    shared_dir: Path,
) -> SynthesizerStats:
    """Run the synthesizer over a single batch of candidates.

    ``llm_call`` is ``(system_prompt, user_message) -> raw_response``.
    Tests inject a deterministic fake. The default caller (see
    :func:`make_anthropic_caller`) wires to Anthropic via the evolve
    bot's API key.
    """
    stats = SynthesizerStats(candidates_read=len(candidates))
    if not candidates:
        return stats

    charter = _load_charter()
    user_msg = _build_user_message(candidates)

    try:
        raw = llm_call(charter, user_msg)
    except Exception as exc:  # noqa: BLE001 — fail soft; next run retries
        stats.errors.append(f"llm_call_failed: {type(exc).__name__}: {exc}")
        log.warning("synthesizer LLM call failed", exc_info=True)
        return stats

    stats.raw_response = raw
    outputs, parse_err = _parse_response(raw)
    if parse_err:
        stats.errors.append(parse_err)
        return stats
    assert outputs is not None

    _route_outputs(outputs, candidates, shared_dir, stats)

    # Append a synthesis log entry for the operator's audit trail.
    _append_synthesis_log(
        shared_dir,
        candidates=candidates,
        stats=stats,
        outputs=outputs,
    )

    return stats


def synthesize_pending(
    shared_dir: Path,
    *,
    llm_call: Callable[[str, str], str],
) -> SynthesizerStats:
    """Convenience wrapper: read everything in synthesizing/, run one
    batch, return stats."""
    candidates = list(
        candidate_store.iter_candidates(shared_dir, subdirs=("synthesizing",))
    )
    return synthesize_batch(
        candidates, llm_call=llm_call, shared_dir=shared_dir
    )


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4 — tool-using agent loop
# ─────────────────────────────────────────────────────────────────────────────


# A tool-using LLM call. Takes the system prompt, the messages array,
# and the tools schema. Returns a dict with this shape (matching the
# Anthropic SDK's response object, but plain dict for test injection):
#
#   {
#     "content": [
#       {"type": "text", "text": "..."},
#       {"type": "tool_use", "id": "tu_...", "name": "...", "input": {...}}
#     ],
#     "stop_reason": "end_turn" | "tool_use" | "max_tokens" | "stop_sequence",
#     "usage": {"input_tokens": int, "output_tokens": int}
#   }
ToolUsingCall = Callable[[str, list[dict], list[dict]], dict]


def _extract_text(content_blocks: list[dict]) -> str:
    parts: list[str] = []
    for block in content_blocks:
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def synthesize_batch_with_tools(
    candidates: list[CandidateProposal],
    *,
    llm_call: ToolUsingCall,
    shared_dir: Path,
    limits: BudgetLimits | None = None,
) -> SynthesizerStats:
    """Run the tool-using synthesizer over a batch (Phase 4).

    Drives a multi-turn agent loop: each turn the model can issue
    tool_use blocks (the synthesizer dispatches them via
    :mod:`proposal_synthesizer.tools`) until it returns the final
    JSON output.

    The :class:`Budget` tracker enforces per-run hard caps from the
    spec §5.2. Reaching a hard cap injects a "wrap up now" message
    and forces a final turn so the model emits whatever it has.

    ``llm_call`` is ``(system_prompt, messages, tools) -> response_dict``.
    The default Anthropic-backed caller is :func:`make_tool_using_caller`;
    tests inject deterministic fakes.
    """
    stats = SynthesizerStats(candidates_read=len(candidates))
    if not candidates:
        return stats

    budget = Budget(limits=limits or DEFAULT_LIMITS)
    budget.start_candidate()  # batch-mode: one "candidate" = whole batch

    charter = _load_charter()
    tools = anthropic_tools_schema()
    messages: list[dict] = [
        {"role": "user", "content": _build_user_message(candidates)}
    ]

    final_text = ""
    forced_wrap = False
    outputs_list: list[dict] | None = None

    for _ in range(ABSOLUTE_TURN_LIMIT):
        # Budget gate BEFORE the next call.
        status = budget.status()
        if status == "hard_cap" and not forced_wrap:
            forced_wrap = True
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "BUDGET STOP — "
                        + budget.status_reason()
                        + ". You are out of investigation budget. Emit "
                        "your final JSON output now (per the charter "
                        "Response format) using whatever you've already "
                        "gathered. Do not request any more tools."
                    ),
                }
            )
        elif status == "soft_warning":
            # Inject a one-time soft nudge, but only when transitioning
            # past the soft cap. The charter has the framing; we keep
            # the nudge lightweight.
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Note: you are past the soft investigation "
                        "target (" + budget.status_reason() + "). "
                        "Wrap up unless you're close to a confident "
                        "conclusion that needs one more tool call."
                    ),
                }
            )
            # Demote so we don't re-inject every turn.
            budget.limits = _bump_soft_caps(budget.limits)

        try:
            response = llm_call(charter, messages, tools)
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(
                f"llm_call_failed: {type(exc).__name__}: {exc}"
            )
            log.warning("synthesizer agent LLM call failed", exc_info=True)
            break

        usage = response.get("usage") or {}
        budget.record_turn(
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
        )

        content = response.get("content") or []
        stop_reason = response.get("stop_reason") or "end_turn"

        if stop_reason == "tool_use" and not forced_wrap:
            # Append assistant turn verbatim, then execute every
            # tool_use block in order and append the tool_result
            # blocks as one user message.
            messages.append({"role": "assistant", "content": content})
            tool_results: list[dict] = []
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                tool_name = block.get("name", "")
                tool_args = block.get("input", {}) or {}
                tool_id = block.get("id", "")
                result = dispatch_tool(tool_name, tool_args, shared_dir)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": json.dumps(result),
                    }
                )
            if not tool_results:
                # Model said stop_reason="tool_use" but no tool_use
                # blocks. Treat as end_turn to avoid an infinite loop.
                final_text = _extract_text(content)
                break
            messages.append({"role": "user", "content": tool_results})
            continue

        # end_turn / max_tokens / stop_sequence / forced wrap — extract
        # the final text and exit the loop.
        final_text = _extract_text(content)
        break
    else:
        stats.errors.append(
            f"agent loop hit ABSOLUTE_TURN_LIMIT ({ABSOLUTE_TURN_LIMIT})"
        )

    stats.raw_response = final_text
    if final_text:
        outputs_list, parse_err = _parse_response(final_text)
        if parse_err:
            stats.errors.append(parse_err)

    if outputs_list is not None:
        _route_outputs(outputs_list, candidates, shared_dir, stats)

    # Synthesis log entry — includes budget snapshot so an operator can
    # see how investigation cost evolved.
    _append_synthesis_log_with_budget(
        shared_dir,
        candidates=candidates,
        stats=stats,
        outputs=outputs_list or [],
        budget=budget,
    )

    return stats


def _bump_soft_caps(limits: BudgetLimits) -> BudgetLimits:
    """Push the soft caps high enough that we don't re-emit the same
    soft-warning every turn. The hard caps remain as walls."""
    from dataclasses import replace

    return replace(
        limits,
        soft_cost_usd_per_candidate=limits.hard_cost_usd_per_candidate,
        soft_turns_per_candidate=limits.hard_turns_per_candidate,
        soft_cost_usd_per_run=limits.hard_cost_usd_per_run,
    )


def _route_outputs(
    outputs: list[dict],
    candidates: list[CandidateProposal],
    shared_dir: Path,
    stats: SynthesizerStats,
) -> None:
    """Dispatch the LLM's outputs list to the appropriate writers.

    Shared between the no-tool (Phase 3) and tool-using (Phase 4)
    paths. Factored here so both use identical post-LLM routing.
    """
    lookup = {c.id: c for c in candidates}
    consumed: set[str] = set()

    for out in outputs:
        if not isinstance(out, dict):
            stats.errors.append("non-dict output entry; skipped")
            continue
        kind = out.get("kind")
        try:
            if kind == "proposal":
                ok, err = _write_proposal_output(out, lookup, shared_dir)
                if ok:
                    stats.proposals_emitted += 1
                    consumed.update(out.get("motivating_candidates") or [])
                else:
                    stats.errors.append(f"proposal write failed: {err}")
            elif kind == "watchlist":
                ok, err = _write_watchlist_output(out, lookup, shared_dir)
                if ok:
                    stats.watchlist_entries += 1
                    consumed.update(out.get("motivating_candidates") or [])
                else:
                    stats.errors.append(f"watchlist write failed: {err}")
            elif kind == "signal_gap":
                ok, err = _write_signal_gap_output(out, lookup, shared_dir)
                if ok:
                    stats.signal_gaps_emitted += 1
                    consumed.update(out.get("motivating_candidates") or [])
                else:
                    stats.errors.append(f"signal_gap write failed: {err}")
            elif kind == "drop":
                ids = out.get("motivating_candidates") or []
                for cid in ids:
                    cand = lookup.get(cid)
                    if cand is None:
                        continue
                    candidate_store.record_drop(
                        shared_dir,
                        cand,
                        reason="synthesizer_dropped",
                        note=out.get("rationale", ""),
                    )
                    candidate_store.delete_candidate(
                        shared_dir, cid, subdir="synthesizing"
                    )
                    consumed.add(cid)
                    stats.drops += 1
            else:
                stats.errors.append(f"unknown output kind: {kind!r}")
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"{kind} handler raised: {exc}")
            log.warning("synthesizer output handler raised", exc_info=True)

    for cid in consumed:
        if lookup.get(cid) is not None:
            candidate_store.delete_candidate(shared_dir, cid, subdir="synthesizing")


def _append_synthesis_log_with_budget(
    shared_dir: Path,
    *,
    candidates: list[CandidateProposal],
    stats: SynthesizerStats,
    outputs: list[dict],
    budget: Budget,
) -> None:
    """Phase 4 synthesis log — same shape as Phase 3's plus a budget snapshot."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "candidate_ids": [c.id for c in candidates],
        "outputs": outputs,
        "stats": {
            "candidates_read": stats.candidates_read,
            "proposals_emitted": stats.proposals_emitted,
            "watchlist_entries": stats.watchlist_entries,
            "signal_gaps_emitted": stats.signal_gaps_emitted,
            "drops": stats.drops,
            "errors": list(stats.errors),
        },
        "budget": budget.snapshot(),
    }
    path = candidate_store.synthesis_log_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def synthesize_pending_with_tools(
    shared_dir: Path,
    *,
    llm_call: ToolUsingCall,
    limits: BudgetLimits | None = None,
) -> SynthesizerStats:
    """Convenience wrapper for Phase 4: read synthesizing/, run the
    tool-using agent, return stats."""
    candidates = list(
        candidate_store.iter_candidates(shared_dir, subdirs=("synthesizing",))
    )
    return synthesize_batch_with_tools(
        candidates, llm_call=llm_call, shared_dir=shared_dir, limits=limits
    )


def make_tool_using_caller(
    api_key: str,
    *,
    model: "str | object" = _USE_TIER2_DEFAULT,
    max_tokens: int = DEFAULT_AGENT_MAX_TOKENS,
) -> ToolUsingCall:
    """Build an Anthropic-backed :class:`ToolUsingCall` for Phase 4.

    Returns a callable matching the ``(system, messages, tools) ->
    response_dict`` contract the agent loop expects.

    ``model`` defaults to a sentinel resolved at construction time to
    the pod's tier2 via :func:`_resolve_pod_anthropic_tier`. Phase 4
    is the investigation/tool-use loop — it needs the workhorse model
    (Sonnet by default) rather than the cheap Phase 3 synthesis tier.
    Pass an explicit string to override.
    """
    from anthropic import Anthropic  # type: ignore[import-not-found]

    if model is _USE_TIER2_DEFAULT:
        model = _resolve_pod_anthropic_tier(
            "tier2", _DEFAULT_AGENT_MODEL_FALLBACK,
        )

    client = Anthropic(api_key=api_key)

    def call(system_prompt: str, messages: list[dict], tools: list[dict]) -> dict:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            tools=tools,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=messages,
        )
        content: list[dict] = []
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                content.append({"type": "text", "text": getattr(block, "text", "")})
            elif btype == "tool_use":
                content.append(
                    {
                        "type": "tool_use",
                        "id": getattr(block, "id", ""),
                        "name": getattr(block, "name", ""),
                        "input": getattr(block, "input", {}) or {},
                    }
                )
        usage = getattr(response, "usage", None)
        return {
            "content": content,
            "stop_reason": getattr(response, "stop_reason", "end_turn"),
            "usage": {
                "input_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
                "output_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
            },
        }

    return call


# ─────────────────────────────────────────────────────────────────────────────
# Synthesis log
# ─────────────────────────────────────────────────────────────────────────────


def _append_synthesis_log(
    shared_dir: Path,
    *,
    candidates: list[CandidateProposal],
    stats: SynthesizerStats,
    outputs: list[dict],
) -> None:
    """Append a single JSONL line summarizing this synthesis run.

    The synthesis log is the operator's audit trail: which candidates
    went in, what came out, what the model's rationale was. Lives at
    ``{shared_dir}/candidates/synthesis_log/<YYYY-MM-DD>.jsonl``.
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "candidate_ids": [c.id for c in candidates],
        "outputs": outputs,
        "stats": {
            "candidates_read": stats.candidates_read,
            "proposals_emitted": stats.proposals_emitted,
            "watchlist_entries": stats.watchlist_entries,
            "signal_gaps_emitted": stats.signal_gaps_emitted,
            "drops": stats.drops,
            "errors": list(stats.errors),
        },
    }
    path = candidate_store.synthesis_log_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# Default LLM caller
# ─────────────────────────────────────────────────────────────────────────────


def make_anthropic_caller(
    api_key: str,
    *,
    model: "str | object" = _USE_TIER3_DEFAULT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> Callable[[str, str], str]:
    """Build an Anthropic-backed ``llm_call`` callable for Phase 3 synthesis.

    The synthesizer runs on the evolve bot's API key per spec §5.1
    ("evolve bot is the universal sysadmin partner"). The key is
    discovered via :func:`resolve_evolve_anthropic_key`; this function
    is the lower-level transport layer.

    ``model`` defaults to a sentinel resolved at construction time to
    the pod's tier3 via :func:`_resolve_pod_anthropic_tier`. Pass an
    explicit string to force a specific id (test fakes, evaluation
    harnesses pinning a comparison model).
    """
    from anthropic import Anthropic  # type: ignore[import-not-found]

    if model is _USE_TIER3_DEFAULT:
        model = _resolve_pod_anthropic_tier("tier3", _DEFAULT_MODEL_FALLBACK)

    client = Anthropic(api_key=api_key)

    def call(system_prompt: str, user_msg: str) -> str:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
        )
        parts: list[str] = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                parts.append(getattr(block, "text", ""))
        return "".join(parts)

    return call


def resolve_evolve_anthropic_key(network: dict[str, Any]) -> str:
    """Find the Anthropic API key the synthesizer authenticates with.

    Spec §5.1: pod-wide engine work runs on the **primary bot's**
    credentials (the bot that actually has a deployed OpenClaw instance +
    provider keys). An ANTHROPIC_API_KEY environment variable, if set,
    takes precedence (operator override; also lets dev / test run without a
    deployed pod).

    The key is resolved from the PRIMARY bot, not the literal ``evolve``
    service account (W10-G round-9): post evo/evolve account-separation the
    ``evolve`` service user has NO OpenClaw instance on Linux — the key
    lives in the primary bot's (``evo``) ``auth-profiles.json``. Resolving
    ``"evolve"`` unconditionally made the daemon report "no Anthropic API
    key resolved" and exit 1 on every fire on a fresh Linux pod, even
    though the key was present (and readable, once the .openclaw ACL mask
    is intact — see secret_config_perms / set_evolve_read_acl). On macOS
    pre-cutover the primary's gateway account IS ``evolve``, and the legacy
    ``primary_bot_id`` fallback is literally ``"evolve"``, so existing pods
    resolve identically.

    Delegates to ``primary_bot.read_primary_bot_anthropic_key`` — the
    dependency-free engine resolver the discovery fit-classifier also uses
    (``ANTHROPIC_API_KEY`` env override → the primary bot's
    ``auth-profiles.json``), so every engine background caller resolves the
    key from one place.
    """
    try:
        from primary_bot import read_primary_bot_anthropic_key
    except ImportError:
        return os.environ.get("ANTHROPIC_API_KEY", "")

    return read_primary_bot_anthropic_key(network)

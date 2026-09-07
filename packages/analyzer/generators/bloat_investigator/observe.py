"""generators.bloat_investigator.observe — Investigate then propose.

Reads firing Signals from the envelope-growth family, gathers
correlated evidence via the investigation toolkit, runs attribution
rules, and emits one Investigation Proposal per investigated bot
carrying the root_cause_attribution block in Provenance.signals.

Design contract (spec §"What 'propose' produces"):

  * One Proposal per bot per run, not per Signal. The investigator's
    job is to *attribute* — emitting three Proposals for three
    correlated Signals would defeat the point.
  * Investigation-shape only in v1 (no L2 applier yet for workspace
    rotation). Attribution still names the cause; the operator
    actions it.
  * Failure semantics: per-bot failures are swallowed so one bad
    payload doesn't torpedo the run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from generators.bloat_investigator.attribution import (
    AttributionResult,
    BloatEvidence,
    attribute,
)
from investigation.proposal_history import (
    operator_already_declined,
    proposal_history,
    summarize_history,
)
from investigation.toolkit import (
    CorrelatedSignal,
    correlated_signals,
    file_top_contributors,
)
from schema.proposal import (
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    new_proposal_id,
)


GENERATOR_ID = "bloat_investigator"
DIMENSION = "cost"

COST_WATCHDOG_PRODUCER = "cost_watchdog"


# ── Dismiss signatures (Phase A.5 + Phase C-1) ──────────────────────────────
#
# Stable per-cause signatures keyed by attribution.cause_key so the
# operator-visible Suppressions list reads "<generator>: <cause>" and a
# dismiss applies to the same *kind* of finding next cycle. Per-bot
# scoping happens at the store layer (record_dismissal carries bot_id).
# Granularity rationale: kind-scoped by cause; not file-scoped because
# the operator who says "stop nagging me about workspace bloat" doesn't
# want to re-dismiss every time a different file trips the rule.
def _dismiss_signature_for(cause_key: str) -> str:
    return f"{GENERATOR_ID}:{cause_key}"

# Signal types the investigator reads. All from cost_watchdog (Phase 1).
CONSUMED_SIGNAL_TYPES = (
    "context_bloat",
    "workspace_growth",
    "cache_envelope_growth",
    "efficiency_drift",
)


@dataclass
class BloatInvestigatorContext:
    """Per-bot run context.

    Attribution rules are pure; the only external read paths are the
    Signal store + the workspace scan. ``config`` is the merged
    network.json + per-bot overrides passed through by the runner.
    """

    bot_id: str
    shared_dir: Path
    config: dict | None = None
    # When True, the investigator suppresses re-emission when the operator
    # has already declined this cause N times. Disabled in tests by default
    # so attribution rule coverage isn't accidentally entangled with the
    # rejection-history check.
    consult_proposal_history: bool = True
    # Phase A.5 — universal dismiss suppression. Skip emission when a
    # matching dismiss entry is active for this bot in the
    # dismissed_signatures store. Granularity is per-cause-key.
    consult_dismissals: bool = True


def _firing_signals(
    shared_dir: Path, bot_id: str
) -> list[CorrelatedSignal]:
    """Pull every firing envelope-growth Signal on this bot."""
    return correlated_signals(
        shared_dir,
        bot_id,
        producer=COST_WATCHDOG_PRODUCER,
        types=list(CONSUMED_SIGNAL_TYPES),
    )


def _bucket_signals(
    sigs: list[CorrelatedSignal],
) -> tuple[list[dict], list[dict], dict | None, list[dict]]:
    """Split firing signals into the four diagnostic buckets the rules read.

    Returns ``(context_bloat_files, growing_files, cache_envelope,
    efficiency_drift_tiers)``. Each bucket holds the relevant Signal
    ``details`` payload; cache_envelope is at most one record (signature
    is per-bot, not per-file).
    """
    bloat: list[dict] = []
    growth: list[dict] = []
    envelope: dict | None = None
    eff: list[dict] = []
    for s in sigs:
        if s.type == "context_bloat":
            bloat.append(s.details)
        elif s.type == "workspace_growth":
            growth.append(s.details)
        elif s.type == "cache_envelope_growth":
            envelope = s.details
        elif s.type == "efficiency_drift":
            eff.append(s.details)
    return bloat, growth, envelope, eff


# ── Phase C-1 operator-first content per cause_key ──────────────────────────
#
# Each cause_key gets its own summary + explanation + action tier. The
# four causes map to two action tiers:
#
#   - growing_memory_drives_envelope    Tier 5 (paste-to-bot)
#   - static_bloat_drives_envelope      Tier 5 (paste-to-bot)
#   - efficiency_drift_without_envelope Tier 2 (UI manual)
#   - ambiguous                         Tier 2 (UI manual)
#
# Tier 5 is right for the file-specific causes because the bot knows
# its own workspace best — pasting an audit instruction is faster than
# the operator clicking through file edits. Tier 2 is right for the
# non-file causes because the operator has to make a model / setup
# call the bot can't make autonomously.


def _largest_file_kb(top_files: list[tuple[str, int]]) -> tuple[str, int]:
    """Return (path, size_kb) of the biggest file or ("", 0) if none."""
    if not top_files:
        return "", 0
    path, size_bytes = top_files[0]
    return path, max(1, int(size_bytes / 1024))


def _phase_b_content_for(
    *,
    bot_id: str,
    cause_key: str,
    top_files: list[tuple[str, int]],
) -> dict:
    """Return the operator-first content fields for this cause.

    Returns a dict with keys: ``summary``, ``explanation``,
    ``action_label``, ``manual_path``, ``manual_instruction``.
    Any field not relevant for the cause's action tier is None.

    Voice rules (spec §"Voice rules"): second person, no snake_case
    slugs, lead with the why, name trade-offs in the explanation.
    """
    biggest_path, biggest_kb = _largest_file_kb(top_files)

    if cause_key == "growing_memory_drives_envelope":
        summary = (
            f"{bot_id} is paying to ship a {biggest_kb} KB workspace file "
            f"into every turn's context, and that file is still growing. "
            f"The producer is usually a heartbeat or cron appending audit "
            f"output. Switching to summary-only output or rotating to "
            f"dated files stops the bleeding."
        ) if biggest_path else (
            f"A workspace file on {bot_id} is being injected into every "
            f"turn's context and is still growing. The producer is "
            f"usually a heartbeat or cron appending audit output."
        )
        explanation = (
            f"Each model turn ships the bot's full context envelope to "
            f"the LLM — system prompt, memory, recent messages, and any "
            f"workspace files the bot has open. Anthropic charges per "
            f"token of that envelope, so anything that grows "
            f"monotonically (audit logs, append-only ledgers) bloats "
            f"every future turn until it's rotated.\n\n"
            f"Diagnosis. We see envelope growth on the bot plus active "
            f"growth on a specific file. The combination is the "
            f"signature of an unbounded append from a heartbeat or cron, "
            f"not a one-off operator paste.\n\n"
            f"What to do. The bot knows its own workspace best — paste "
            f"the instruction below and it will audit the file, identify "
            f"the producer (heartbeat, cron, manual edit), and propose a "
            f"rotation or summary-only refactor. If the bot is fully "
            f"automated and can't act on prose, the same checklist works "
            f"for a human walking the file manually.\n\n"
            f"What could go wrong. Truncating a memory file the bot is "
            f"actively reading can lose context the bot was leaning on. "
            f"Rotate to dated files first (keeping the recent days "
            f"around) before trimming aggressively; watch the next "
            f"day's behavior for regressions."
        )
        manual_instruction = (
            f"Audit the largest growing file in your workspace "
            f"({biggest_path or 'check your workspace top-N report'}). "
            f"Identify what's appending to it — heartbeat, cron, or "
            f"manual writes — and propose either (a) switching the "
            f"producer to summary-only output, (b) rotating to dated "
            f"files that stop being referenced after 7 days, or (c) "
            f"trimming the current file with a clear retention rule. "
            f"Explain the trade-offs of each option. If you can act "
            f"on the rotation yourself, do so and report what changed."
        )
        return {
            "summary": summary,
            "explanation": explanation,
            "action_label": None,  # Investigation default button
            "manual_path": None,
            "manual_instruction": manual_instruction,
        }

    if cause_key == "static_bloat_drives_envelope":
        summary = (
            f"{bot_id} is paying to ship a {biggest_kb} KB file into "
            f"every turn even though the file isn't growing. It might "
            f"be intentional context — or it might be a leftover the "
            f"bot doesn't actually need anymore."
        ) if biggest_path else (
            f"A large file in {bot_id}'s workspace is being shipped "
            f"into every turn's context. It isn't growing, but its "
            f"size is the dominant cost contributor."
        )
        explanation = (
            f"Every model turn ships the bot's full workspace context. "
            f"A large file that doesn't change still gets re-sent on "
            f"every call, so a 50 KB file open all day quietly adds "
            f"to the per-turn cost.\n\n"
            f"Diagnosis. The envelope is large but stable — not "
            f"actively growing. That points at a file the operator (or "
            f"an earlier setup step) left in the workspace and the bot "
            f"keeps shipping. Common causes: a backup snapshot, an "
            f"old onboarding doc, or a vendored corpus that should "
            f"have moved to retrieval.\n\n"
            f"What to do. Decide whether the file is load-bearing "
            f"context the bot actually uses, or leftover material. If "
            f"it's leftover, trim or move it out of workspace. If it's "
            f"intentional, raise the per-file threshold so this "
            f"finding stops firing — there's no cost-free way to keep "
            f"shipping the file, but at least the engine will stop "
            f"nagging you about a decision you've already made.\n\n"
            f"What could go wrong. Moving a file the bot relies on "
            f"will change its behavior. If you're unsure, ask the bot "
            f"what it uses the file for before removing — pasting the "
            f"instruction below routes the question to the bot itself."
        )
        manual_instruction = (
            f"Look at {biggest_path or 'the largest file in your workspace'} "
            f"and tell me what you use it for. If you don't actively "
            f"reference it, propose moving it out of your workspace "
            f"context. If it's load-bearing context, explain why and "
            f"whether a smaller summary would serve the same purpose."
        )
        return {
            "summary": summary,
            "explanation": explanation,
            "action_label": None,  # Investigation default button
            "manual_path": None,
            "manual_instruction": manual_instruction,
        }

    if cause_key == "efficiency_drift_without_envelope":
        summary = (
            f"{bot_id}'s per-call cost is climbing but the context "
            f"envelope is stable — the extra cost isn't coming from "
            f"larger inputs. Most likely the primary model was swapped "
            f"recently, or output lengths grew."
        )
        explanation = (
            f"Two things drive per-call cost: how much you send "
            f"(envelope) and how much the model returns (output). If "
            f"the envelope is stable and cost still climbs, the change "
            f"is on the output side or in the model's price sheet.\n\n"
            f"Diagnosis. The cost watchdog flagged efficiency drift "
            f"without a matching envelope-growth signal. The two usual "
            f"culprits: (1) the bot's primary model was changed — "
            f"swapping from Haiku to Sonnet roughly doubles per-token "
            f"cost — or (2) output_tokens grew because something is "
            f"asking the bot for longer answers.\n\n"
            f"What to do. Open the cost optimization page for "
            f"{bot_id} and check whether the primary model was changed "
            f"recently. Then look at output_tokens per trigger kind in "
            f"the cost ledger — a single trigger getting verbose can "
            f"shift the average per-call cost without changing average "
            f"envelope size.\n\n"
            f"What could go wrong. Reverting a model swap might "
            f"regress quality on the workload the swap was meant to "
            f"serve. Confirm the swap was deliberate before changing "
            f"back; the cost signal might be a real trade-off, not a "
            f"mistake."
        )
        return {
            "summary": summary,
            "explanation": explanation,
            "action_label": "Open Cost Optimization",
            "manual_path": f"Cost Optimization → {bot_id}",
            "manual_instruction": None,
        }

    # ambiguous
    summary = (
        f"Several envelope-growth signals are firing on {bot_id} but no "
        f"single root cause is clear. The largest workspace file is "
        f"the most common starting point; the evidence in Details lays "
        f"out everything we gathered."
    )
    explanation = (
        f"Bloat investigations work by correlating multiple cost "
        f"signals — workspace growth, context bloat, cache envelope, "
        f"efficiency drift — and naming the most likely root cause. "
        f"Sometimes the signals don't line up: growth without bloat, "
        f"or vice versa.\n\n"
        f"Diagnosis. The combination of signals firing doesn't match "
        f"any single rule with confidence, so we surface the gathered "
        f"evidence and let the operator decide.\n\n"
        f"What to do. Open Cost Optimization for the bot and look at "
        f"the largest workspace file first — that's the most common "
        f"starting point for envelope-driven cost. If the file is "
        f"familiar and expected, look at the efficiency-drift evidence "
        f"in Details to see whether the model was swapped recently.\n\n"
        f"What could go wrong. Acting on an ambiguous finding without "
        f"the diagnosis is guessing. Take a minute to review the "
        f"Details — the cost is real, but the wrong fix can make "
        f"things worse."
    )
    return {
        "summary": summary,
        "explanation": explanation,
        "action_label": "Open Cost Optimization",
        "manual_path": f"Cost Optimization → {bot_id}",
        "manual_instruction": None,
    }


def _build_proposal(
    bot_id: str,
    attr: AttributionResult,
    sigs: list[CorrelatedSignal],
    top_files: list[tuple[str, int]],
    history_summary: Any = None,
) -> Proposal:
    """Compose the Investigation Proposal from the attribution + evidence."""
    motivating_ids = [s.signal_id for s in sigs if s.signal_id]
    sig_types = sorted({s.type for s in sigs})

    # Operator-facing body. Lead with the cause, then the evidence, then
    # the actionable suggestion. Team_bot_a-style: short header + one fact per
    # line + close-out — per feedback_message_style_team_bot_a_like.
    lines: list[str] = []
    lines.append(attr.headline)
    lines.append("")
    lines.append("**What's firing on this bot right now:**")
    for sig_type in sig_types:
        lines.append(f"- `{sig_type}`")
    lines.append("")

    if attr.cause_key != "ambiguous":
        lines.append("**Likely root cause:**")
        lines.append(f"`{attr.cause_key}` (confidence {attr.confidence:.0%})")
        lines.append("")

    if attr.primary_target:
        lines.append(f"**Where to look first:** `{attr.primary_target}`")
        lines.append("")

    if top_files:
        lines.append("**Largest workspace files on this bot:**")
        for path, size in top_files[:5]:
            lines.append(f"- {path} — {size / 1024:.0f} KB")
        lines.append("")

    if history_summary is not None and history_summary.total > 0:
        lines.append(
            f"**Prior proposals for this cause:** {history_summary.total} "
            f"(declined {history_summary.declined}, "
            f"approved {history_summary.approved})"
        )
        if history_summary.most_recent_status:
            lines.append(
                f"Most recent: {history_summary.most_recent_status} "
                f"on {history_summary.most_recent_created_at[:10]}"
            )
        lines.append("")

    if attr.cause_key == "growing_memory_drives_envelope":
        target = attr.primary_target or "the file above"
        lines.append("**What to do:**")
        lines.append(
            f"1. Inspect `{target}` — the producer is usually a heartbeat "
            "or cron appending audit output."
        )
        lines.append(
            "2. Switch the producer to summary-only output, or rotate to "
            "dated files that stop being referenced after N days."
        )
        lines.append(
            f"3. Trim or archive the current file. Once the next "
            f"cost_watchdog sweep sees a smaller envelope, the firing "
            f"Signals auto-archive."
        )
    elif attr.cause_key == "static_bloat_drives_envelope":
        target = attr.primary_target or "the file above"
        lines.append("**What to do:**")
        lines.append(
            f"1. Inspect `{target}` — it isn't currently growing, but "
            "it's oversized and still in the cache envelope every call."
        )
        lines.append(
            "2. Trim, rotate, or move out of workspace if it's not "
            "intentional context."
        )
        lines.append(
            f"3. If intentional, raise the per-file threshold via "
            f"`cost_watchdog.bots.{bot_id}.context_bloat_files.{target}`."
        )
    elif attr.cause_key == "efficiency_drift_without_envelope":
        lines.append("**What to do:**")
        lines.append(
            "1. Check whether the bot's primary model changed recently "
            "(openclaw.json::agents.defaults.model.primary) — the "
            "envelope is stable so this is most likely a model swap."
        )
        lines.append(
            "2. If model is unchanged, check output_tokens trend per "
            "trigger_kind via cost_ledger — long outputs scale per-call "
            "cost the same way an envelope does."
        )
    else:  # ambiguous
        lines.append(
            "**Operator triage:** the envelope-growth signal family is "
            "firing but no single root cause is clear. The evidence "
            "block below has everything we gathered; the largest file "
            "is the most common starting point."
        )

    body = "\n".join(lines)

    # Pack the full root_cause_attribution block into Provenance.signals.
    # Spec §"What 'propose' produces": every proposal carries this block.
    provenance_signals = {
        "primary_signal_types": sig_types,
        "primary_signal_signatures":
            sorted({s.signature for s in sigs if s.signature}),
        "root_cause_attribution": {
            "cause_key": attr.cause_key,
            "headline": attr.headline,
            "confidence": attr.confidence,
            "primary_target": attr.primary_target,
            "evidence": attr.evidence,
        },
        "top_files_kb":
            [{"path": p, "size_kb": int(s / 1024)} for p, s in top_files[:5]],
    }
    if history_summary is not None:
        provenance_signals["history"] = {
            "total": history_summary.total,
            "declined": history_summary.declined,
            "approved": history_summary.approved,
            "most_recent_status": history_summary.most_recent_status,
        }

    problem = attr.headline
    # Map cause_key (generator-rule identifier, used for calibration and
    # tracking) → operator-facing phrase. The cause_key still travels in
    # provenance.signals + the proposal body — only the headline gets
    # the humanized version. 2026-06-02 humanization sweep; the previous
    # template "Investigate {bot} envelope growth — {cause_key}" was
    # called out as a worst offender (raw rule names in the title).
    _CAUSE_PHRASE = {
        "growing_memory_drives_envelope":
            "growing memory file is bloating every turn's context",
        "static_bloat_drives_envelope":
            "a workspace file is bloating every turn's context",
        "efficiency_drift_without_envelope":
            "per-call cost is climbing without a clear cache cause",
        "ambiguous":
            "context envelope is growing — needs operator triage",
    }
    cause_phrase = _CAUSE_PHRASE.get(
        attr.cause_key, "investigate growing context envelope",
    )
    headline_short = f"{bot_id}: {cause_phrase}"[:120]

    # ── Phase C-1 operator-first content ────────────────────────────────────
    content = _phase_b_content_for(
        bot_id=bot_id, cause_key=attr.cause_key, top_files=top_files,
    )

    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"{t}:{bot_id}" for t in sig_types],
        provenance=Provenance(
            technique=f"bloat_investigator.{attr.cause_key}",
            signals=provenance_signals,
            confidence=attr.confidence,
        ),
        problem=problem,
        action=Investigation(context=body),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary=headline_short,
        motivating_signals=motivating_ids,
        # ── Phase C-1 (2026-06-04 protocol) ────────────────────────────
        summary=content["summary"],
        explanation=content["explanation"],
        action_label=content["action_label"],
        manual_path=content["manual_path"],
        manual_instruction=content["manual_instruction"],
        dismiss_signature=_dismiss_signature_for(attr.cause_key),
        dismiss_scope="kind",
    )


def observe(ctx: BloatInvestigatorContext) -> list[Proposal]:
    """Investigate envelope-growth Signals on this bot; emit one Proposal.

    Per-bot only; if no relevant Signals are firing, returns ``[]`` — the
    investigator never produces a proposal for a healthy bot. When at
    least one envelope-growth Signal is firing, we always emit at least
    one Proposal (ambiguous is still actionable).
    """
    if ctx.shared_dir is None or not ctx.bot_id:
        return []

    try:
        sigs = _firing_signals(ctx.shared_dir, ctx.bot_id)
    except Exception:
        return []
    if not sigs:
        return []

    bloat, growth, envelope, eff = _bucket_signals(sigs)
    try:
        top_files_fs = file_top_contributors(
            ctx.bot_id, n=5, config=ctx.config
        )
    except Exception:
        top_files_fs = []
    top_files = [(f.path, f.size_bytes) for f in top_files_fs]

    evidence = BloatEvidence(
        bot_id=ctx.bot_id,
        primary_signal_type=sigs[0].type,
        primary_signal_signature=sigs[0].signature,
        context_bloat_files=bloat,
        growing_files=growth,
        cache_envelope=envelope,
        efficiency_drift_tiers=eff,
        top_files=top_files,
    )
    attribution = attribute(evidence)

    # Suppress re-emission when the operator has already declined this
    # cause repeatedly. The proposal is noise at that point — operator
    # decision stands. Skipped when consult_proposal_history is False
    # (test isolation) or attribution is ambiguous (the operator can't
    # have declined an unnamed cause).
    if (
        ctx.consult_proposal_history
        and attribution.cause_key != "ambiguous"
        and operator_already_declined(
            ctx.shared_dir,
            bot_id=ctx.bot_id,
            cause_key=attribution.cause_key,
            min_recent_declines=2,
        )
    ):
        return []

    # Look up past proposals for this cause (regardless of decline count)
    # so the proposal body can show recurrence context.
    history_entries: list = []
    if ctx.consult_proposal_history and attribution.cause_key != "ambiguous":
        try:
            history_entries = proposal_history(
                ctx.shared_dir,
                bot_id=ctx.bot_id,
                cause_key=attribution.cause_key,
                limit=10,
            )
        except Exception:
            history_entries = []

    history_summary = summarize_history(
        history_entries, bot_id=ctx.bot_id, cause_key=attribution.cause_key,
    ) if attribution.cause_key != "ambiguous" else None

    # Phase A.5 dismiss-suppression gate. Honor a kind-scoped dismiss
    # for this cause on this bot (or pod-wide). Granularity is per
    # cause_key — dismissing "memory bloat" doesn't suppress "static
    # bloat" for the same bot.
    if ctx.consult_dismissals and _is_dismissed(
        ctx.shared_dir, ctx.bot_id, attribution.cause_key,
    ):
        return []

    try:
        return [_build_proposal(
            ctx.bot_id, attribution, sigs, top_files, history_summary,
        )]
    except Exception:
        return []


def _is_dismissed(shared_dir: Path, bot_id: str, cause_key: str) -> bool:
    """Return True if the operator has dismissed this cause for this bot.

    Fail-open: any import or read failure returns False so a broken
    dismissals sidecar can't suppress legitimate proposals. Honors
    pod-wide entries (bot_id=None in the store matches every bot)
    via dismissals.is_suppressed's own per-bot logic.
    """
    try:
        from arbiter.dismissals import is_suppressed
    except ImportError:
        return False
    try:
        return is_suppressed(
            shared_dir,
            signature=_dismiss_signature_for(cause_key),
            bot_id=bot_id,
        )
    except Exception:
        return False

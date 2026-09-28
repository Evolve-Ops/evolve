"""generators.exec_outcome_investigator.observe — Investigate then propose.

Reads firing exec_outcome_watchdog Signals on the bot, runs attribution
rules, emits one Investigation Proposal carrying the root_cause_attribution
block. Same shape as bloat_investigator — different evidence inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from generators.exec_outcome_investigator.attribution import (
    AttributionResult,
    ExecOutcomeEvidence,
    attribute,
)
from investigation.proposal_history import (
    operator_already_declined,
    proposal_history,
    summarize_history,
)
from investigation.toolkit import (
    CorrelatedSignal,
    ManifestMention,
    correlated_signals,
    manifest_mentions,
)
from schema.proposal import (
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    UpdateExecApproval,
    new_proposal_id,
)


GENERATOR_ID = "exec_outcome_investigator"
DIMENSION = "safety"

EXEC_OUTCOME_PRODUCER = "exec_outcome_watchdog"

CONSUMED_SIGNAL_TYPES = (
    "tool_error_burst",
    "exec_denied",
    "approval_timeout",
    "preflight_block",
)


@dataclass
class ExecOutcomeInvestigatorContext:
    bot_id: str
    shared_dir: Path
    config: dict | None = None
    consult_proposal_history: bool = True


def _firing_signals(
    shared_dir: Path, bot_id: str
) -> list[CorrelatedSignal]:
    return correlated_signals(
        shared_dir,
        bot_id,
        producer=EXEC_OUTCOME_PRODUCER,
        types=list(CONSUMED_SIGNAL_TYPES),
    )


def _bucket_signals(
    sigs: list[CorrelatedSignal],
) -> tuple[dict | None, list[dict], dict | None, dict | None]:
    """Return (tool_error_burst, exec_denied_list, approval_timeout, preflight_block).

    burst / timeout / preflight are at-most-one (signature is per-bot);
    exec_denied is a list because the signature is per-(bot, tool).
    """
    burst: dict | None = None
    denied: list[dict] = []
    timeout: dict | None = None
    preflight: dict | None = None
    for s in sigs:
        if s.type == "tool_error_burst":
            burst = s.details
        elif s.type == "exec_denied":
            denied.append(s.details)
        elif s.type == "approval_timeout":
            timeout = s.details
        elif s.type == "preflight_block":
            preflight = s.details
    return burst, denied, timeout, preflight


def _select_action_and_risk(
    bot_id: str,
    attr: AttributionResult,
    body: str,
) -> tuple[Any, RiskTag]:
    """Pick the right Action and RiskTag for the cause.

    Phase 3 default: ``exec_denied_allowlist_gap`` with a non-empty
    ``suggested_pattern`` → ``UpdateExecApproval(add)`` via the existing
    L2 applier.

    Phase 4 (shipped 2026-05-28): ``approval_timeout_recurring_tool``
    uses the same one-click fix shape — when the same tool keeps timing
    out, the structural fix is the same as for repeated denials.

    Everything else stays Investigation. When the suggested pattern is
    empty (tool_name was generic or missing), we fall back to
    Investigation — emitting an UpdateExecApproval with an empty pattern
    would be a broken proposal.
    """
    _AUTONOMOUS_FIX_CAUSES = {
        "exec_denied_allowlist_gap",
        "approval_timeout_recurring_tool",
    }
    if attr.cause_key in _AUTONOMOUS_FIX_CAUSES:
        pattern = (attr.evidence or {}).get("suggested_pattern") or ""
        if isinstance(pattern, str) and pattern:
            action = UpdateExecApproval(
                bot_id=bot_id,
                operation="add",
                pattern=pattern,
                agent_id="main",
                scope="agent",
            )
            risk = RiskTag(
                blast_radius="bot",
                reversibility="auto",
                touches=["auth_config"],
            )
            return action, risk
    # All other causes (and the fallback): Investigation, no state change.
    action = Investigation(context=body)
    risk = RiskTag(blast_radius="bot", reversibility="manual", touches=[])
    return action, risk


def _build_proposal(
    bot_id: str,
    attr: AttributionResult,
    sigs: list[CorrelatedSignal],
    history_summary: Any = None,
    manifest_matches: list[ManifestMention] | None = None,
) -> Proposal:
    motivating_ids = [s.signal_id for s in sigs if s.signal_id]
    sig_types = sorted({s.type for s in sigs})

    lines: list[str] = [attr.headline, ""]
    lines.append("**Exec-outcome Signals firing on this bot:**")
    for sig_type in sig_types:
        lines.append(f"- `{sig_type}`")
    lines.append("")

    if attr.cause_key != "ambiguous":
        lines.append(
            f"**Likely root cause:** `{attr.cause_key}` "
            f"(confidence {attr.confidence:.0%})"
        )
        lines.append("")

    if attr.primary_target:
        lines.append(f"**Primary target:** `{attr.primary_target}`")
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

    # Per-cause actionable section. Mirrors bloat_investigator's pattern
    # — give the operator the next step in the operator's own language,
    # not a wall of evidence.
    if attr.cause_key == "exec_denied_allowlist_gap":
        target = attr.primary_target or "the tool above"
        suggested = (attr.evidence or {}).get("suggested_pattern") or ""
        if suggested:
            if manifest_matches:
                # Lead with the manifest cross-ref — this is mechanically-
                # wrong state, not just a recommendation.
                first = manifest_matches[0]
                more = (
                    f" + {len(manifest_matches) - 1} more"
                    if len(manifest_matches) > 1 else ""
                )
                lines.append(
                    f"**Manifest declares this capability.** App "
                    f"`{first.app_name}` ({first.manifest_path}){more} "
                    f"mentions `{target}` — the system has been told "
                    f"the bot should be able to do this. Exec policy "
                    f"saying no is a mechanically-wrong state, not a "
                    f"policy choice."
                )
                lines.append("")
            lines.append("**One-click fix:**")
            lines.append(
                f"This proposal applies an `UpdateExecApproval(add)` with "
                f"pattern `{suggested}`. The applier writes "
                f"`/Users/{bot_id}/.openclaw/exec-approvals.json` (L2 path: "
                "/tmp stage + sudo /bin/cp + chmod 644 + gateway kickstart)."
            )
            lines.append("")
            lines.append("**Before approving:**")
            lines.append(
                "1. Verify the pattern path matches where the tool actually "
                "lives on the bot — `/usr/bin/<tool>*` is the default; some "
                "tools live in `/bin` (e.g. ps) or `/usr/local/bin` (e.g. "
                "language runtimes). Refine the pattern if the path is wrong."
            )
            lines.append(
                "2. Confirm this is a capability the bot SHOULD have — the "
                "L2 applier auto-rejects against the denylist regex, but "
                "operator judgment is the first line of defense."
            )
            lines.append(
                "3. After approval, the gateway kickstarts and OC picks up "
                "the new allowlist within seconds; the bot's next exec "
                "attempt will succeed."
            )
        else:
            lines.append("**What to do:**")
            lines.append(
                f"1. Inspect `/Users/{bot_id}/.openclaw/exec-approvals.json` "
                "and check whether the allowlist includes the blocked tool."
            )
            lines.append(
                "2. If the bot's manifest (INSTALLED_APPS.md) declares this "
                "capability, extend the allowlist to permit it."
            )
            lines.append(
                "3. The tool name from the denial was too generic for an "
                "automated pattern — operator-only path applies here."
            )
    elif attr.cause_key == "approval_timeout_recurring_tool":
        target = attr.primary_target or "the tool above"
        suggested = (attr.evidence or {}).get("suggested_pattern") or ""
        count = (attr.evidence or {}).get("primary_tool_timeout_count") or 0
        threshold = (
            (attr.evidence or {}).get("recurrence_threshold") or 3
        )
        lines.append("**Why this is a one-click fix, not a routing fix:**")
        lines.append(
            f"`{target}` has timed out {count} times over the window — "
            f"above the recurrence threshold ({threshold}×). Even if the "
            f"operator notices the next request, approving the same "
            f"command repeatedly burns 30 minutes of bot stall time per "
            f"retry. The structural fix is to remove the approval "
            f"requirement for this specific command."
        )
        lines.append("")
        if suggested:
            lines.append("**One-click fix:**")
            lines.append(
                f"This proposal applies an `UpdateExecApproval(add)` with "
                f"pattern `{suggested}`. Same L2 applier path as the "
                f"`exec_denied_allowlist_gap` cause — `/Users/{bot_id}"
                f"/.openclaw/exec-approvals.json` gets a new entry, "
                f"gateway kickstarts, and the bot's next run on this "
                f"command succeeds without prompting."
            )
            lines.append("")
            lines.append("**Before approving:**")
            lines.append(
                "1. Verify the pattern path matches where the tool actually "
                "lives on the bot — `/usr/bin/<tool>*` is the default."
            )
            lines.append(
                "2. Confirm this is a capability the bot SHOULD have — "
                "operator judgment, not the L2 denylist regex, is the "
                "first line of defense."
            )
            lines.append(
                "3. If the recurrence is from a misbehaving cron/heartbeat "
                "(not a legitimate capability), dismiss this proposal and "
                "fix the cron instead."
            )
        else:
            lines.append("**What to do:**")
            lines.append(
                f"1. Inspect `/Users/{bot_id}/.openclaw/exec-approvals.json` "
                f"and check whether the pattern for `{target}` is present."
            )
            lines.append(
                "2. The tool name was too generic for an automated "
                "pattern — operator decides whether to add it manually."
            )
    elif attr.cause_key == "approval_timeout_operator_missed":
        per_tool = (attr.evidence or {}).get("per_tool_counts") or {}
        tools_str = ", ".join(
            f"`{t}`({c})" for t, c in sorted(
                per_tool.items(), key=lambda kv: kv[1], reverse=True,
            )[:5]
        ) if per_tool else ""
        lines.append("**Why this is a routing fix, not an allowlist fix:**")
        if tools_str:
            lines.append(
                f"Timeouts are scattered across multiple tools — "
                f"{tools_str}. No single tool dominates "
                f"(recurrence threshold is 3×), so extending the "
                f"allowlist for any one wouldn't materially reduce "
                f"the prompt rate. The underlying problem is "
                f"visibility — the operator isn't seeing approval "
                f"requests inside OC's 30-min TTL."
            )
        else:
            lines.append(
                "Timeouts are scattered across multiple tools. The "
                "underlying problem is visibility, not any single "
                "capability gap."
            )
        lines.append("")
        lines.append("**What to do:**")
        lines.append(
            "1. Confirm which channel routes this bot's approval requests "
            "(Telegram DM, admin UI push, OC terminal)."
        )
        lines.append(
            "2. If you're not actively watching that surface, enable a "
            "faster channel (admin UI push or evo DM) so requests arrive "
            "inside OC's 30-min approval TTL."
        )
        lines.append(
            "3. For specific recurring tasks, individual "
            "`UpdateExecApproval` proposals will emit as their tool "
            "counts climb above the recurrence threshold."
        )
    elif attr.cause_key == "preflight_block_known":
        lines.append("**What to do:**")
        lines.append(
            "1. Inspect the failing command shape — OC v5.26+ blocks "
            "`python`, `node`, pipes, `&&`, `>`, `-c` regardless of allowlist."
        )
        lines.append(
            "2. Wrap the command in a single-purpose script under the "
            f"bot's `~/.openclaw/workspace/scripts/` and point the bot at the script."
        )
        lines.append(
            "3. Track the upstream OC fix at "
            "https://github.com/openclaw/openclaw/issues/87371"
        )
    elif attr.cause_key == "tool_error_burst_unclassified":
        target = attr.primary_target or "the worst session"
        lines.append("**What to do:**")
        lines.append(
            f"1. Open the worst session (id `{target[:8] if target else '—'}`) "
            "and read the failing tool calls."
        )
        lines.append(
            "2. If the failure mode matches a known shape (denied / "
            "timeout / preflight) but the content-matcher missed it, file "
            "an issue to extend the pattern list."
        )
        lines.append(
            "3. If the failures are runtime (network, MCP unreachable), "
            "the right fix is integration-level, not exec-policy."
        )
    else:  # ambiguous
        lines.append(
            "**Operator triage:** exec-outcome Signals fired but no "
            "specific failure mode latched. The evidence block below "
            "has what we gathered; the worst session is the right "
            "starting point."
        )

    body = "\n".join(lines)

    provenance_signals: dict = {
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
    }
    if history_summary is not None:
        provenance_signals["history"] = {
            "total": history_summary.total,
            "declined": history_summary.declined,
            "approved": history_summary.approved,
            "most_recent_status": history_summary.most_recent_status,
        }
    if manifest_matches:
        provenance_signals["manifest_declares_tool"] = True
        provenance_signals["declaring_apps"] = [
            {
                "app_id": m.app_id,
                "app_name": m.app_name,
                "manifest_path": m.manifest_path,
                "snippet": m.snippet,
            }
            for m in manifest_matches
        ]

    action, risk_tag = _select_action_and_risk(bot_id, attr, body)
    # Operator-facing fields differ between the autonomous-fix path
    # (UpdateExecApproval) and the operator-decides path (Investigation).
    # For UpdateExecApproval, the body text is moved into ``problem``
    # because the Action type has no ``context`` field — the admin UI's
    # generic kv-card renderer would drop the rationale otherwise. The
    # problem field is rendered above the Action panel so the operator
    # sees the cause + recommended action before the structured data.
    if getattr(action, "kind", "") == "UpdateExecApproval":
        headline_short = (
            f"Allow {action.pattern} for {bot_id}"
        )[:120]
        proposal_problem = body
        # Manifest cross-ref bumps urgency: when the bot's installed-app
        # system has been told it should be able to do X and exec policy
        # says no, that's a mechanically-wrong state, not a discretionary
        # improvement. Surface it accordingly. Phase 4 (smarter-generators
        # spec) deferred this; landed via the manifest cross-ref work.
        urgency = "critical" if manifest_matches else "improvement"
    else:
        headline_short = (
            f"Investigate {bot_id} exec outcomes — {attr.cause_key}"
        )[:120]
        proposal_problem = attr.headline
        urgency = "improvement"

    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"{t}:{bot_id}" for t in sig_types],
        provenance=Provenance(
            technique=f"exec_outcome_investigator.{attr.cause_key}",
            signals=provenance_signals,
            confidence=attr.confidence,
        ),
        problem=proposal_problem,
        action=action,
        risk_tag=risk_tag,
        claim=None,
        approval_audience="pod_operator",
        urgency=urgency,
        admin_surface_summary=headline_short,
        motivating_signals=motivating_ids,
        # Per-cause dismiss so dismissing one root cause doesn't suppress
        # findings about a different cause on the same bot.
        dismiss_signature=f"exec_outcome_investigator:{attr.cause_key}",
        dismiss_scope="kind",
    )


def observe(ctx: ExecOutcomeInvestigatorContext) -> list[Proposal]:
    if ctx.shared_dir is None or not ctx.bot_id:
        return []
    try:
        sigs = _firing_signals(ctx.shared_dir, ctx.bot_id)
    except Exception:
        return []
    if not sigs:
        return []

    burst, denied, timeout, preflight = _bucket_signals(sigs)
    evidence = ExecOutcomeEvidence(
        bot_id=ctx.bot_id,
        primary_signal_type=sigs[0].type,
        primary_signal_signature=sigs[0].signature,
        tool_error_burst=burst,
        exec_denied=denied,
        approval_timeout=timeout,
        preflight_block=preflight,
    )
    attribution = attribute(evidence)

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
        history_entries,
        bot_id=ctx.bot_id,
        cause_key=attribution.cause_key,
    ) if attribution.cause_key != "ambiguous" else None

    # Manifest cross-ref: when the autonomous-fix path is recommending
    # an allowlist extension, check whether the bot's installed-app
    # manifests already declare this tool. A match is mechanically-
    # wrong-state evidence (system says bot should be able to do X,
    # exec policy says no) and the body framing + urgency reflects
    # that. No match → urgency stays "improvement" (the bot might
    # legitimately not be supposed to have this capability).
    manifest_matches: list[ManifestMention] = []
    if attribution.cause_key == "exec_denied_allowlist_gap":
        primary_tool = (attribution.evidence or {}).get("primary_tool") or ""
        if primary_tool and primary_tool != "exec":
            try:
                manifest_matches = manifest_mentions(
                    ctx.bot_id, primary_tool,
                )
            except Exception:
                manifest_matches = []

    try:
        return [_build_proposal(
            ctx.bot_id, attribution, sigs, history_summary, manifest_matches,
        )]
    except Exception:
        return []

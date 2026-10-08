"""generators.exec_outcome_investigator.attribution — Pure attribution rules.

Each rule is a pure function ``(evidence) -> AttributionResult | None``.
First match wins; ambiguous fallback when no rule fires.

Rule order encodes "more specific first" — same discipline as
bloat_investigator. A wide rule that catches too much defeats the
calibration loop because we can't measure individual rule accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AttributionResult:
    cause_key: str
    headline: str
    confidence: float
    primary_target: str = ""  # tool name when applicable
    evidence: dict = field(default_factory=dict)


@dataclass
class ExecOutcomeEvidence:
    """Inputs the rules read. Composed by observe() from firing Signals."""

    bot_id: str
    # Primary signal that triggered this investigation
    primary_signal_type: str
    primary_signal_signature: str
    # Bucketed Signal details payloads. Each is a list because there
    # can be multiple (e.g. multiple distinct tools denied).
    tool_error_burst: dict | None = None
    exec_denied: list[dict] = field(default_factory=list)
    approval_timeout: dict | None = None
    preflight_block: dict | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Rules
# ─────────────────────────────────────────────────────────────────────────────


# OC's exec-approvals.json patterns are typically absolute-path globs (e.g.
# `/usr/bin/curl*`, `/bin/ps*`). Most system tools live in /usr/bin so we
# default to that prefix; the operator can refine before approving if the
# tool lives elsewhere. Verified live on security_bot 2026-05-28: existing
# allowlist entries use this shape.
_DEFAULT_BIN_PREFIX = "/usr/bin"


def _suggested_pattern_for_tool(tool_name: str) -> str:
    """Build a conservative exec-approval pattern from a tool name.

    Pattern is ``<bin_prefix>/<tool>*`` — glob asterisk so any arguments
    match, but path-qualified so OC's canonical-path matcher accepts it.
    Operator can refine the pattern via the proposal refine flow before
    approving (some tools live in /bin or /usr/local/bin and the
    operator-side review catches that).
    """
    if not tool_name or tool_name == "exec":
        # No tool name to anchor on — operator must supply the pattern
        # manually. Return empty so the caller can fall back to
        # Investigation rather than emitting a broken UpdateExecApproval.
        return ""
    # Strip any path the tool name might already carry; we want just the
    # leaf so the bin_prefix is unambiguous.
    leaf = tool_name.rsplit("/", 1)[-1]
    return f"{_DEFAULT_BIN_PREFIX}/{leaf}*"


def rule_exec_denied_allowlist_gap(
    evidence: ExecOutcomeEvidence,
) -> AttributionResult | None:
    """exec_denied signals firing → fixable allowlist gap.

    Phase 3 (shipped 2026-05-28): rule populates a `suggested_pattern`
    in evidence so observe.py can emit an UpdateExecApproval(add) action
    targeting the bot's exec-approvals.json. When the suggested pattern
    can't be derived (tool name missing / generic), the proposal falls
    back to Investigation in observe.py.

    Picks the highest-count denied tool as the primary target. Confidence
    is high — when OC says "denied" the cause is unambiguous.
    """
    if not evidence.exec_denied:
        return None
    by_count = sorted(
        evidence.exec_denied,
        key=lambda d: int(d.get("denial_count") or 0),
        reverse=True,
    )
    top = by_count[0]
    target = str(top.get("tool_name") or "")
    distinct_tools = [
        d.get("tool_name", "") for d in evidence.exec_denied
    ]
    suggested_pattern = _suggested_pattern_for_tool(target)
    return AttributionResult(
        cause_key="exec_denied_allowlist_gap",
        headline=(
            f"{evidence.bot_id}: tool `{target}` repeatedly denied by exec policy — "
            f"likely missing from the allowlist"
        ),
        confidence=0.9,
        primary_target=target,
        evidence={
            "primary_tool": target,
            "primary_tool_denial_count": top.get("denial_count"),
            "primary_sample_result_preview":
                top.get("sample_result_preview"),
            "primary_sample_input_preview":
                top.get("sample_input_preview"),
            "distinct_tools_denied": distinct_tools,
            "total_denial_count":
                sum(int(d.get("denial_count") or 0) for d in evidence.exec_denied),
            "suggested_pattern": suggested_pattern,
        },
    )


# Phase 4 (shipped 2026-05-28): how many timeouts on a single tool counts
# as "recurring" — at that point the right fix is the same as exec_denied
# (extend the allowlist) rather than a notification-channel change. Three
# is the default: once a tool has timed out 3+ times the operator clearly
# isn't going to see future requests on the same cadence, and OC's
# 30-min TTL means each retry burns 30 minutes of bot stall time. Lower
# than three has too much noise; higher than three lets the bot stall
# longer than necessary.
RECURRENCE_TOOL_THRESHOLD = 3


def rule_approval_timeout_recurring_tool(
    evidence: ExecOutcomeEvidence,
) -> AttributionResult | None:
    """approval_timeout where ONE tool is dominating → allowlist gap.

    Phase 4. When the same `(bot, tool)` pair keeps timing out, the
    operator's notification channel isn't the root cause — even if they
    saw the request, they'd be approving the same command over and over.
    The structural fix is to add the tool to the allowlist so the
    approval prompt isn't needed. Same shape as exec_denied_allowlist_gap;
    emits an UpdateExecApproval via the suggested_pattern evidence key.

    Fires when ``approval_timeout.details.max_single_tool_count >=
    RECURRENCE_TOOL_THRESHOLD``. Otherwise the timeouts are scattered
    across multiple tools and the next rule
    (``rule_approval_timeout_operator_missed``) handles them as a
    routing problem.
    """
    if evidence.approval_timeout is None:
        return None
    payload = evidence.approval_timeout
    max_single = int(payload.get("max_single_tool_count") or 0)
    if max_single < RECURRENCE_TOOL_THRESHOLD:
        return None
    top_tool = str(payload.get("top_tool") or "")
    if not top_tool or top_tool == "exec":
        # No identifiable tool to extend the allowlist for — defer to the
        # scattered/routing rule, which surfaces the evidence without
        # proposing a broken UpdateExecApproval.
        return None
    # Reuse the same pattern derivation as exec_denied so the operator
    # workflow is identical across the two recurrence shapes.
    from generators.exec_outcome_investigator.attribution import (
        _suggested_pattern_for_tool,
    )
    suggested_pattern = _suggested_pattern_for_tool(top_tool)
    per_tool_counts = payload.get("per_tool_counts") or {}
    return AttributionResult(
        cause_key="approval_timeout_recurring_tool",
        headline=(
            f"{evidence.bot_id}: tool `{top_tool}` approval timed out "
            f"{max_single}× — extend the allowlist instead of waiting for "
            f"the next approval window"
        ),
        confidence=0.85,
        primary_target=top_tool,
        evidence={
            "primary_tool": top_tool,
            "primary_tool_timeout_count": max_single,
            "total_timeout_count": payload.get("timeout_count"),
            "distinct_tools": payload.get("distinct_tools"),
            "per_tool_counts": per_tool_counts,
            "sample_session_id": payload.get("sample_session_id"),
            "sample_result_preview": payload.get("sample_result_preview"),
            "recurrence_threshold": RECURRENCE_TOOL_THRESHOLD,
            "suggested_pattern": suggested_pattern,
        },
    )


def rule_approval_timeout_operator_missed(
    evidence: ExecOutcomeEvidence,
) -> AttributionResult | None:
    """approval_timeout firing → operator-routing problem.

    The bot asked, the operator didn't see it inside the 30-min TTL,
    OC quietly dropped the request. This is almost always a notification
    channel issue — not a config one.

    Phase 4 reshape: this rule now handles the *scattered* case (timeouts
    across multiple tools, no single tool dominating). When one tool
    dominates, the recurring-tool rule above catches it and emits an
    UpdateExecApproval instead.
    """
    if evidence.approval_timeout is None:
        return None
    payload = evidence.approval_timeout
    return AttributionResult(
        cause_key="approval_timeout_operator_missed",
        headline=(
            f"{evidence.bot_id}: {payload.get('timeout_count', 0)} exec approval(s) "
            f"timed out across {len(payload.get('distinct_tools') or [])} "
            f"tools — likely a notification-routing problem"
        ),
        confidence=0.75,
        primary_target="",
        evidence={
            "timeout_count": payload.get("timeout_count"),
            "distinct_tools": payload.get("distinct_tools"),
            "per_tool_counts": payload.get("per_tool_counts"),
            "sample_session_id": payload.get("sample_session_id"),
            "sample_result_preview": payload.get("sample_result_preview"),
            "next_step": (
                "operator-routing fix — enable faster approval channel "
                "(admin UI push, evo DM) so requests arrive inside OC's "
                "30-min TTL. Extending the allowlist is also an option but "
                "the spread across tools suggests the underlying problem is "
                "visibility, not any single capability gap."
            ),
        },
    )


def rule_preflight_block_known(
    evidence: ExecOutcomeEvidence,
) -> AttributionResult | None:
    """preflight_block firing → known OC v5.26+ behavior.

    OC's command parser blocks `python`/`node`/pipes/`&&`/`>`/`-c`
    regardless of allowlist. Fix is rephrasing (wrap in script) or
    waiting for the upstream OC change in openclaw#87371.
    """
    if evidence.preflight_block is None:
        return None
    payload = evidence.preflight_block
    return AttributionResult(
        cause_key="preflight_block_known",
        headline=(
            f"{evidence.bot_id}: {payload.get('block_count', 0)} command(s) blocked "
            f"by OC preflight — see openclaw#87371"
        ),
        confidence=0.85,
        primary_target="",
        evidence={
            "block_count": payload.get("block_count"),
            "distinct_tools": payload.get("distinct_tools"),
            "sample_input_preview": payload.get("sample_input_preview"),
            "sample_result_preview": payload.get("sample_result_preview"),
            "upstream_issue": "openclaw/openclaw#87371",
        },
    )


def rule_tool_error_burst_unclassified(
    evidence: ExecOutcomeEvidence,
) -> AttributionResult | None:
    """tool_error_burst fires alone — Modes 2/3/4 didn't classify.

    The annotation pipeline counted errors but content inspection didn't
    match denial / timeout / preflight signatures. Either the failures
    are runtime (network, MCP unreachable) or the content-matcher needs
    a new pattern. Low-confidence; ask the operator to inspect.
    """
    if evidence.tool_error_burst is None:
        return None
    if evidence.exec_denied or evidence.approval_timeout or evidence.preflight_block:
        return None  # let the more-specific rules handle
    payload = evidence.tool_error_burst
    return AttributionResult(
        cause_key="tool_error_burst_unclassified",
        headline=(
            f"{evidence.bot_id}: {payload.get('tool_error_total', 0)} tool errors "
            f"but no specific failure mode matched"
        ),
        confidence=0.4,
        primary_target=str(payload.get("worst_session_id") or ""),
        evidence={
            "tool_error_total": payload.get("tool_error_total"),
            "sessions_with_errors": payload.get("sessions_with_errors"),
            "worst_session_id": payload.get("worst_session_id"),
            "worst_session_errors": payload.get("worst_session_errors"),
            "likely_runtime_or_pattern_gap": True,
            "next_step": (
                "inspect the worst session manually; if the failures match "
                "a known OC pattern not yet caught, file a pattern-update issue"
            ),
        },
    )


def attribute(evidence: ExecOutcomeEvidence) -> AttributionResult:
    """Run rules in order; fall through to ambiguous.

    Rule precedence: denied (most actionable) → recurring-tool timeout
    (Phase 4, same fix as denied) → scattered-tools timeout (routing
    fix) → preflight (upstream issue) → unclassified burst (operator
    inspect) → ambiguous (surface evidence). The recurring-tool rule
    must run BEFORE the operator-missed rule — if both could fire, we
    want the more-actionable allowlist fix to win.
    """
    for rule in (
        rule_exec_denied_allowlist_gap,
        rule_approval_timeout_recurring_tool,
        rule_approval_timeout_operator_missed,
        rule_preflight_block_known,
        rule_tool_error_burst_unclassified,
    ):
        result = rule(evidence)
        if result is not None:
            return result
    return AttributionResult(
        cause_key="ambiguous",
        headline=(
            f"{evidence.bot_id}: exec-outcome Signals present without a clear cause"
        ),
        confidence=0.3,
        primary_target="",
        evidence={
            "primary_signal_type": evidence.primary_signal_type,
            "has_burst": evidence.tool_error_burst is not None,
            "denied_tools": [
                d.get("tool_name") for d in evidence.exec_denied
            ],
            "has_timeout": evidence.approval_timeout is not None,
            "has_preflight": evidence.preflight_block is not None,
        },
    )

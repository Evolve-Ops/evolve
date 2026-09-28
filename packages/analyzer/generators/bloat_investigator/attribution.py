"""generators.bloat_investigator.attribution — Pure root-cause rules.

Spec: internal/spec-smarter-generators-2026-05-28.md §"What 'attribute' is".

Each rule is a pure function ``(evidence) -> AttributionResult | None``.
The investigator runs them in order; first match wins. ``None`` means
"this rule didn't fit; try the next one." When no rule matches, the
caller returns a sentinel "ambiguous" result so the operator gets the
evidence even without a named cause.

Rules are narrow on purpose. A wide rule that catches too much breaks
the calibration loop (Phase 3) — we want each rule's accuracy to be
measurable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class AttributionResult:
    """Named root-cause attribution emitted by a rule.

    ``cause_key`` is the rule identifier — calibration tracks success rate
    per cause_key over time. ``confidence`` is 0..1; "high" = all expected
    evidence present, "medium" = most, "low" = enough to name but uncertain.
    ``primary_target`` is the most actionable thing the operator should
    look at (usually a file path); empty when the rule is meta (e.g.
    "ambiguous"). ``evidence`` is a free-form dict — the proposal body
    serializes it into the root_cause_attribution block.
    """

    cause_key: str
    headline: str
    confidence: float
    primary_target: str = ""
    evidence: dict = field(default_factory=dict)


@dataclass
class BloatEvidence:
    """Inputs the rules read. Composed by the observe() step from the
    investigation toolkit + the primary signal payload.

    Field naming mirrors the Signal types so a reader can map back:
    ``context_bloat_files`` are the files firing context_bloat,
    ``growing_files`` are firing workspace_growth, etc.
    """

    bot_id: str
    # The signal that triggered this investigation
    primary_signal_type: str
    primary_signal_signature: str
    # Correlated signal payloads — all dicts of details.
    context_bloat_files: list[dict] = field(default_factory=list)
    growing_files: list[dict] = field(default_factory=list)
    cache_envelope: Optional[dict] = None
    efficiency_drift_tiers: list[dict] = field(default_factory=list)
    # File-size landscape on the bot, for the rules that need it.
    top_files: list[tuple[str, int]] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Attribution rules
# ─────────────────────────────────────────────────────────────────────────────


def rule_growing_memory_drives_envelope(
    evidence: BloatEvidence,
) -> AttributionResult | None:
    """Triad: growing file + envelope growth + (optionally) efficiency drift.

    The Security_bot-shape signature. When all three fire on the same bot, the
    attribution is structural: a workspace file is climbing, the cache
    envelope is climbing with it, and the per-call cost has caught up.
    The file at the top of file_top_contributors is the actionable target.
    """
    if not evidence.growing_files:
        return None
    if evidence.cache_envelope is None:
        return None

    # Pick the fastest-growing file as the primary target — that's the
    # one operator's lever moves the needle on.
    growing = sorted(
        evidence.growing_files,
        key=lambda d: float(d.get("growth_kb_per_day") or 0),
        reverse=True,
    )
    target = growing[0].get("filename") or ""

    has_efficiency = bool(evidence.efficiency_drift_tiers)
    confidence = 0.9 if has_efficiency else 0.75
    return AttributionResult(
        cause_key="growing_memory_drives_envelope",
        headline=(
            f"{evidence.bot_id}: workspace file growing → cache envelope inflating → "
            f"per-call cost following"
            if has_efficiency
            else f"{evidence.bot_id}: workspace file growing → cache envelope inflating"
        ),
        confidence=confidence,
        primary_target=target,
        evidence={
            "growing_file": target,
            "growing_file_kb_per_day": growing[0].get("growth_kb_per_day"),
            "growing_file_current_kb": growing[0].get("current_kb"),
            "cache_envelope_ratio": evidence.cache_envelope.get("ratio"),
            "cache_envelope_cur_tokens_per_call":
                evidence.cache_envelope.get("cur_tokens_per_call"),
            "efficiency_drift_present": has_efficiency,
            "efficiency_drift_tiers":
                [d.get("tier") for d in evidence.efficiency_drift_tiers],
        },
    )


def rule_static_bloat_drives_envelope(
    evidence: BloatEvidence,
) -> AttributionResult | None:
    """context_bloat + cache_envelope_growth without a growth-rate signal.

    The file is *already* oversized and the envelope is paying for it,
    but the file isn't currently growing — maybe it was bloated at some
    point and then stabilized. Same operator action (rotate/trim), but
    the framing is "this is here; it shouldn't be" rather than "it's
    climbing."
    """
    if not evidence.context_bloat_files:
        return None
    if evidence.cache_envelope is None:
        return None
    if evidence.growing_files:
        return None  # let the growing-rule handle this case

    bloat_files = sorted(
        evidence.context_bloat_files,
        key=lambda d: float(d.get("size_kb") or 0),
        reverse=True,
    )
    target = bloat_files[0].get("filename") or ""
    return AttributionResult(
        cause_key="static_bloat_drives_envelope",
        headline=(
            f"{evidence.bot_id}: oversized file still injecting into envelope — "
            f"trim or rotate"
        ),
        confidence=0.75,
        primary_target=target,
        evidence={
            "bloat_file": target,
            "bloat_file_size_kb": bloat_files[0].get("size_kb"),
            "cache_envelope_ratio": evidence.cache_envelope.get("ratio"),
            "cache_envelope_cur_tokens_per_call":
                evidence.cache_envelope.get("cur_tokens_per_call"),
        },
    )


def rule_efficiency_drift_without_envelope(
    evidence: BloatEvidence,
) -> AttributionResult | None:
    """efficiency_drift fires, but cache envelope is stable.

    The bot's per-call cost is up but it isn't envelope-driven. Most
    likely a model swap (high-tier model now used where low-tier used to
    be) or an output-length change. Either way, the operator needs to
    investigate something different from the envelope path — and the
    bloat-investigator shouldn't pretend it knows the cause. Surface
    the right pointer.
    """
    if not evidence.efficiency_drift_tiers:
        return None
    if evidence.cache_envelope is not None:
        return None
    if evidence.growing_files or evidence.context_bloat_files:
        return None
    return AttributionResult(
        cause_key="efficiency_drift_without_envelope",
        headline=(
            f"{evidence.bot_id}: per-call cost climbing but envelope stable — "
            f"model swap or output growth"
        ),
        confidence=0.6,
        primary_target="",
        evidence={
            "efficiency_drift_tiers":
                [d.get("tier") for d in evidence.efficiency_drift_tiers],
            "ratios": [d.get("ratio") for d in evidence.efficiency_drift_tiers],
            "envelope_stable": True,
            "suggested_next": (
                "check model used per turn (cost_watchdog.detect_model_override_violated) "
                "or output_tokens trend (cost_ledger rollup by trigger_kind)"
            ),
        },
    )


def attribute(evidence: BloatEvidence) -> AttributionResult:
    """Run the rules in order; first match wins. Falls through to ambiguous.

    Rule order encodes "more specific first":
      1. growing + envelope (most actionable, highest confidence)
      2. static bloat + envelope (still actionable, different framing)
      3. drift without envelope (different problem entirely; redirect)
      4. ambiguous (surface evidence, let operator decide)
    """
    for rule in (
        rule_growing_memory_drives_envelope,
        rule_static_bloat_drives_envelope,
        rule_efficiency_drift_without_envelope,
    ):
        result = rule(evidence)
        if result is not None:
            return result
    return AttributionResult(
        cause_key="ambiguous",
        headline=(
            f"{evidence.bot_id}: envelope-growth signal present without "
            f"clear root cause — surfacing evidence for operator triage"
        ),
        confidence=0.3,
        primary_target=(evidence.top_files[0][0] if evidence.top_files else ""),
        evidence={
            "primary_signal_type": evidence.primary_signal_type,
            "context_bloat_count": len(evidence.context_bloat_files),
            "growing_count": len(evidence.growing_files),
            "cache_envelope_present": evidence.cache_envelope is not None,
            "efficiency_drift_count": len(evidence.efficiency_drift_tiers),
            "top_files":
                [{"path": p, "size_kb": int(s / 1024)} for p, s in evidence.top_files[:5]],
        },
    )

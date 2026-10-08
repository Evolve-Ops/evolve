"""Regression tests for cascade.preflight_audit.

The audit grades pre-flight intent router decisions against turn
outcomes — categorizes each span into agreement / over_escalation /
under_escalation / cascade_corrected / overridden / abstained, then
rolls up per-bot rates that audit_runner emits as Signals.

These tests pin:

  - categorize_span() correctness for each category
  - _is_trivial / _is_struggle threshold semantics
  - _find_cascade_corrections finds turn-pairs across multi-turn sessions
  - compute_preflight_stats produces the expected shape + correct rates
  - audit_runner._collect_preflight_disagreement_signals emits at the
    right rate thresholds with the right severity / details payload

Spec: internal/spec-preflight-intent-router-2026-06-06.md.
"""

from __future__ import annotations

import sys
from pathlib import Path


_ANALYZER_DIR = Path(__file__).resolve().parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from cascade.preflight_audit import (  # noqa: E402
    CAT_AGREEMENT,
    CAT_OVER_ESCALATION,
    CAT_UNDER_ESCALATION,
    CAT_CASCADE_CORRECTED,
    CAT_OVERRIDDEN,
    CAT_ABSTAINED,
    CAT_NOT_RUN,
    GRADED_CATEGORIES,
    PREFLIGHT_MIN_DECISIONS,
    PREFLIGHT_OVER_ESCALATION_THRESHOLD,
    PREFLIGHT_UNDER_ESCALATION_THRESHOLD,
    PREFLIGHT_CASCADE_CORRECTED_THRESHOLD,
    _find_cascade_corrections,
    _is_struggle,
    _is_trivial,
    categorize_span,
    compute_preflight_stats,
)
from cascade.audit_runner import (  # noqa: E402
    PRODUCER,
    _collect_preflight_disagreement_signals,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _span(
    *,
    bot_id: str = "team_bot_a",
    session_id: str = "sess-1",
    turn_index: int = 1,
    preflight_tier: str | None = None,
    preflight_reason: str = "regex:design_imperative",
    preflight_layer: str | None = "regex",
    tier_used: str | None = None,
    tier_chosen_by: str = "preflight",
    success: bool | None = True,
    struggle_score: float = 0.0,
    tool_count: int = 0,
    total_cost: float = 0.01,
    output_tokens: int = 100,
    latency_ms: float = 0.5,
) -> dict:
    """Build a synthetic cascade-span dict with pre-flight attributes."""
    attrs: dict = {
        "session_id": session_id,
        "turn_index": turn_index,
        "cascade.tier_used": tier_used if tier_used else preflight_tier,
        "cascade.tier_chosen_by": tier_chosen_by,
        "cascade.struggle.score": struggle_score,
        "cascade.struggle.raw.tool_count_per_turn": tool_count,
    }
    if preflight_layer is not None:
        attrs["cascade.preflight.tier"] = preflight_tier
        attrs["cascade.preflight.reason"] = preflight_reason
        attrs["cascade.preflight.layer"] = preflight_layer
        attrs["cascade.preflight.confidence"] = 1.0
        attrs["cascade.preflight.latency_ms"] = latency_ms
    if success is not None:
        attrs["cascade.success"] = success
    return {
        "bot_id": bot_id,
        "trace_id": session_id,
        "attributes": attrs,
        "total_cost": total_cost,
        "usage": {"output_tokens": output_tokens},
    }


# ── _is_trivial ─────────────────────────────────────────────────────────────


def test_is_trivial_returns_true_for_short_cheap_successful_turn():
    s = _span(
        total_cost=0.02, output_tokens=80, tool_count=0, success=True,
    )
    assert _is_trivial(s) is True


def test_is_trivial_false_when_cost_above_threshold():
    s = _span(total_cost=0.06, output_tokens=80, tool_count=0, success=True)
    assert _is_trivial(s) is False


def test_is_trivial_false_when_output_long():
    s = _span(total_cost=0.02, output_tokens=300, tool_count=0, success=True)
    assert _is_trivial(s) is False


def test_is_trivial_false_when_multi_tool():
    s = _span(total_cost=0.02, output_tokens=80, tool_count=3, success=True)
    assert _is_trivial(s) is False


def test_is_trivial_false_when_failure_flag():
    # A failed short turn is NOT trivial — failure IS the signal.
    s = _span(total_cost=0.02, output_tokens=80, tool_count=0, success=False)
    assert _is_trivial(s) is False


# ── _is_struggle ────────────────────────────────────────────────────────────


def test_is_struggle_true_when_score_above_threshold():
    s = _span(struggle_score=0.55)
    assert _is_struggle(s) is True


def test_is_struggle_true_when_failure_flag():
    s = _span(struggle_score=0.0, success=False)
    assert _is_struggle(s) is True


def test_is_struggle_true_when_tier3_cost_above_threshold():
    # Tier3 turn that cost > $0.20 → struggle signal (long context)
    s = _span(total_cost=0.30, struggle_score=0.0, success=True)
    assert _is_struggle(s) is True


def test_is_struggle_false_when_clean_short_turn():
    s = _span(struggle_score=0.1, success=True, total_cost=0.05)
    assert _is_struggle(s) is False


# ── categorize_span ─────────────────────────────────────────────────────────


def test_categorize_not_run_when_router_did_not_fire():
    # Heartbeat / cron / subagent — preflight_layer absent from span
    s = _span(preflight_layer=None)
    assert categorize_span(s, set()) == CAT_NOT_RUN


def test_categorize_abstained_when_router_had_no_opinion():
    s = _span(preflight_layer="abstain", preflight_tier=None,
              tier_chosen_by="classifier")
    assert categorize_span(s, set()) == CAT_ABSTAINED


def test_categorize_overridden_when_higher_priority_driver_won():
    # Pre-flight said tier1 but operator chip forced tier3 → not a
    # misrouting, expected behavior, excluded from rates.
    s = _span(
        preflight_tier="tier1", preflight_layer="regex",
        tier_chosen_by="user_request", tier_used="tier3",
    )
    assert categorize_span(s, set()) == CAT_OVERRIDDEN


def test_categorize_agreement_on_meaningful_tier1_turn():
    # Tier1 turn that did real work — pre-flight was right to escalate
    s = _span(
        preflight_tier="tier1", tier_used="tier1",
        total_cost=2.50, output_tokens=5000, tool_count=8,
    )
    assert categorize_span(s, set()) == CAT_AGREEMENT


def test_categorize_over_escalation_on_trivial_tier1_turn():
    # Tier1 turn that was trivially handled — wasted Opus pricing
    s = _span(
        preflight_tier="tier1", tier_used="tier1",
        total_cost=0.02, output_tokens=80, tool_count=0, success=True,
    )
    assert categorize_span(s, set()) == CAT_OVER_ESCALATION


def test_categorize_under_escalation_on_struggling_tier3_turn():
    # Tier3 turn that struggled — user might have benefited from tier2/1
    s = _span(
        preflight_tier="tier3", tier_used="tier3",
        struggle_score=0.7, success=False,
    )
    assert categorize_span(s, set()) == CAT_UNDER_ESCALATION


def test_categorize_agreement_on_clean_tier3_turn():
    s = _span(
        preflight_tier="tier3", tier_used="tier3",
        struggle_score=0.0, success=True, total_cost=0.005,
    )
    assert categorize_span(s, set()) == CAT_AGREEMENT


def test_categorize_tier2_always_agreement():
    # tier2 IS the default workhorse — neither over nor under by definition
    s = _span(preflight_tier="tier2", tier_used="tier2", total_cost=0.5)
    assert categorize_span(s, set()) == CAT_AGREEMENT


def test_categorize_cascade_corrected_overrides_agreement():
    # Even if this turn looks fine on its own, the NEXT turn was cascade-
    # escalated → pre-flight under-shot for the session as a whole
    s = _span(
        preflight_tier="tier2", tier_used="tier2",
        session_id="sess-X", turn_index=1,
    )
    corrections = {("sess-X", 1)}
    assert categorize_span(s, corrections) == CAT_CASCADE_CORRECTED


# ── _find_cascade_corrections ───────────────────────────────────────────────


def test_find_cascade_corrections_identifies_turn_pairs():
    spans = [
        _span(session_id="multi-1", turn_index=1, preflight_tier="tier2",
              tier_used="tier2", tier_chosen_by="preflight"),
        _span(session_id="multi-1", turn_index=2,
              tier_used="tier1", tier_chosen_by="cascade"),
    ]
    corrections = _find_cascade_corrections(spans)
    assert ("multi-1", 1) in corrections
    assert ("multi-1", 2) not in corrections


def test_find_cascade_corrections_ignores_non_cascade_escalations():
    # Turn 2 went to tier1 but the driver was user_request, not cascade —
    # pre-flight didn't get "corrected," the user just asked for power.
    spans = [
        _span(session_id="multi-2", turn_index=1, preflight_tier="tier2",
              tier_used="tier2"),
        _span(session_id="multi-2", turn_index=2,
              tier_used="tier1", tier_chosen_by="user_request"),
    ]
    corrections = _find_cascade_corrections(spans)
    assert ("multi-2", 1) not in corrections


def test_find_cascade_corrections_empty_for_single_turn_sessions():
    spans = [
        _span(session_id="solo", turn_index=1,
              preflight_tier="tier2", tier_used="tier2"),
    ]
    assert _find_cascade_corrections(spans) == set()


def test_find_cascade_corrections_ignores_tier1_to_tier1():
    # If pre-flight was ALREADY tier1, a tier1 next-turn isn't a correction
    spans = [
        _span(session_id="t1", turn_index=1, preflight_tier="tier1",
              tier_used="tier1"),
        _span(session_id="t1", turn_index=2,
              tier_used="tier1", tier_chosen_by="cascade"),
    ]
    assert _find_cascade_corrections(spans) == set()


# ── compute_preflight_stats ─────────────────────────────────────────────────


def test_stats_empty_population():
    out = compute_preflight_stats([])
    assert out["by_bot"] == {}
    assert out["totals"]["spans_seen"] == 0


def test_stats_groups_by_bot_and_excludes_not_run():
    spans = [
        # team_bot_a: 1 user_turn with preflight
        _span(bot_id="team_bot_a", preflight_tier="tier1", tier_used="tier1",
              total_cost=2.0, output_tokens=3000, tool_count=5),
        # team_bot_a: 1 heartbeat (no preflight)
        _span(bot_id="team_bot_a", preflight_layer=None, tier_used="tier3"),
        # team_bot_b: 1 user_turn with preflight
        _span(bot_id="team_bot_b", preflight_tier="tier3", tier_used="tier3",
              session_id="t2", struggle_score=0.0, total_cost=0.005),
    ]
    out = compute_preflight_stats(spans)
    a = out["by_bot"]["team_bot_a"]
    assert a["total_spans"] == 2
    assert a["preflight_ran"] == 1
    assert a["decisions"] == 1
    assert a["categories"][CAT_AGREEMENT] == 1
    b = out["by_bot"]["team_bot_b"]
    assert b["decisions"] == 1
    assert b["categories"][CAT_AGREEMENT] == 1


def test_stats_rates_computed_correctly():
    spans = []
    # 6 over-escalations + 4 agreements = 10 decisions, 60% over-escalation
    for i in range(6):
        spans.append(_span(
            bot_id="b1", session_id=f"s-over-{i}",
            preflight_tier="tier1", tier_used="tier1",
            total_cost=0.01, output_tokens=50, tool_count=0,
        ))
    for i in range(4):
        spans.append(_span(
            bot_id="b1", session_id=f"s-ok-{i}",
            preflight_tier="tier1", tier_used="tier1",
            total_cost=2.0, output_tokens=3000, tool_count=5,
        ))
    out = compute_preflight_stats(spans)
    b = out["by_bot"]["b1"]
    assert b["decisions"] == 10
    assert b["rates"]["over_escalation_rate"] == 0.6
    assert b["rates"]["agreement_rate"] == 0.4


def test_stats_by_layer_breakdown():
    spans = [
        _span(bot_id="b1", session_id="s1", preflight_tier="tier1",
              preflight_layer="regex", preflight_reason="regex:design_imperative",
              tier_used="tier1", total_cost=2.0, output_tokens=3000, tool_count=5),
        _span(bot_id="b1", session_id="s2", preflight_tier="tier3",
              preflight_layer="haiku", preflight_reason="haiku:tier3",
              tier_used="tier3", total_cost=0.005),
    ]
    out = compute_preflight_stats(spans)
    by_layer = out["by_bot"]["b1"]["by_layer"]
    assert by_layer["regex"]["count"] == 1
    assert by_layer["regex"]["agreement"] == 1
    assert by_layer["haiku"]["count"] == 1
    assert by_layer["haiku"]["agreement"] == 1


def test_stats_by_reason_breakdown_for_top_offender_attribution():
    """Per-reason stats are how Phase 4 RSI attributes misroutings to
    specific rules. A regex pattern that over-escalates 80% of its hits
    should be findable here."""
    spans = []
    # 5 design_imperative hits, all over-escalations
    for i in range(5):
        spans.append(_span(
            bot_id="b1", session_id=f"de-{i}",
            preflight_tier="tier1", tier_used="tier1",
            preflight_reason="regex:design_imperative",
            total_cost=0.01, output_tokens=50, tool_count=0,
        ))
    # 2 weigh_options hits, both agreements
    for i in range(2):
        spans.append(_span(
            bot_id="b1", session_id=f"wo-{i}",
            preflight_tier="tier1", tier_used="tier1",
            preflight_reason="regex:weigh_options",
            total_cost=1.5, output_tokens=2000, tool_count=3,
        ))
    out = compute_preflight_stats(spans)
    reasons = out["by_bot"]["b1"]["by_reason"]
    assert reasons["regex:design_imperative"]["count"] == 5
    assert reasons["regex:design_imperative"][CAT_OVER_ESCALATION] == 5
    assert reasons["regex:weigh_options"]["count"] == 2
    assert reasons["regex:weigh_options"][CAT_AGREEMENT] == 2


def test_stats_haiku_cost_projection():
    spans = [
        _span(bot_id="b1", session_id=f"h-{i}",
              preflight_layer="haiku", preflight_tier="tier2",
              preflight_reason="haiku:tier2", tier_used="tier2",
              latency_ms=120 + i)
        for i in range(10)
    ]
    out = compute_preflight_stats(spans)
    b = out["by_bot"]["b1"]
    assert b["haiku_call_count"] == 10
    # 10 calls × $0.00015 = $0.0015
    assert b["haiku_estimated_cost_usd"] == 0.0015
    assert b["haiku_latency_ms_p50"] is not None


# ── audit_runner._collect_preflight_disagreement_signals ───────────────────


def _make_spans(category: str, bot_id: str = "b1", n: int = 30) -> list[dict]:
    """Helper: produce N spans of a given graded category for one bot."""
    spans = []
    for i in range(n):
        if category == CAT_OVER_ESCALATION:
            spans.append(_span(
                bot_id=bot_id, session_id=f"{category}-{i}",
                preflight_tier="tier1", tier_used="tier1",
                total_cost=0.01, output_tokens=50, tool_count=0,
            ))
        elif category == CAT_UNDER_ESCALATION:
            spans.append(_span(
                bot_id=bot_id, session_id=f"{category}-{i}",
                preflight_tier="tier3", tier_used="tier3",
                struggle_score=0.7, success=False,
            ))
        else:  # CAT_AGREEMENT
            spans.append(_span(
                bot_id=bot_id, session_id=f"{category}-{i}",
                preflight_tier="tier1", tier_used="tier1",
                total_cost=2.0, output_tokens=3000, tool_count=5,
            ))
    return spans


def test_signal_fires_when_over_escalation_above_threshold():
    # 30 over-escalations + 70 agreements = 30% over-escalation rate
    spans = _make_spans(CAT_OVER_ESCALATION, n=30)
    spans.extend(_make_spans(CAT_AGREEMENT, n=70))
    sigs = _collect_preflight_disagreement_signals(spans)
    over = [s for s in sigs if s["type"] == "preflight_over_escalation"]
    assert len(over) == 1
    assert over[0]["producer"] == PRODUCER
    assert over[0]["severity"] == "warn"
    assert over[0]["details"]["over_escalation_count"] == 30
    # 30% > 15% threshold
    assert over[0]["details"]["over_escalation_rate"] > PREFLIGHT_OVER_ESCALATION_THRESHOLD


def test_signal_silent_below_threshold():
    # 3 over-escalations + 27 agreements = 10% — below 15% threshold
    spans = _make_spans(CAT_OVER_ESCALATION, n=3)
    spans.extend(_make_spans(CAT_AGREEMENT, n=27))
    sigs = _collect_preflight_disagreement_signals(spans)
    over = [s for s in sigs if s["type"] == "preflight_over_escalation"]
    assert over == []


def test_signal_silent_when_sample_below_sparse_floor():
    # 4 decisions, all over-escalations (100% rate) → sample < SPARSE_MIN
    # (5). Truly too small to draw any conclusion; suppressed entirely.
    spans = _make_spans(CAT_OVER_ESCALATION, n=4)
    sigs = _collect_preflight_disagreement_signals(spans)
    assert sigs == []


# ── Sparse-bot tier (2026-06-08): low-volume installs ──────────────────────
#
# Pre-fix, bots with <30 graded decisions in the audit window produced
# zero Signals regardless of rate. Household / personal-assistant bots
# rarely accumulate 30 graded decisions in any reasonable window, so
# the audit layer was structurally blind to their misroutings. The sparse-bot tier catches the same patterns at lower
# confidence (severity="info" instead of "warn") with a higher rate
# threshold (25%) and an absolute-count floor (≥3) to keep single-
# turn noise from triggering false alarms.


def test_signal_sparse_fires_at_info_severity():
    # 8 decisions, 4 over-escalations = 50% rate. Above sparse threshold
    # (25%), absolute count (4) above floor (3), decisions in [5, 30).
    spans = _make_spans(CAT_OVER_ESCALATION, n=4)
    spans.extend(_make_spans(CAT_AGREEMENT, n=4))
    sigs = _collect_preflight_disagreement_signals(spans)
    over = [s for s in sigs if s["type"] == "preflight_over_escalation"]
    assert len(over) == 1
    # Sparse path fires at info, not warn — operator gets a heads-up,
    # not an urgent alert (sample size doesn't support high confidence).
    assert over[0]["severity"] == "info"
    # Body should include the "small sample" qualifier
    assert "small sample" in over[0]["body"]
    # Details should mark the sparse-path lineage so downstream consumers
    # (UI, RSI) can distinguish high-confidence vs sparse-path signals.
    assert over[0]["details"]["sparse_sample"] is True
    # Threshold field reports the sparse threshold (25%), not the
    # high-volume 15% — the operator should see what the rate cleared.
    assert over[0]["details"]["threshold"] == 0.25


def test_signal_sparse_silent_when_absolute_count_too_low():
    # 10 decisions, 2 over-escalations = 20% rate. Above sparse rate
    # threshold (25%)? No — 20% < 25%. But also fails count floor (2 < 3).
    # Either failure alone blocks emission; both fail here. Either way:
    # silent.
    spans = _make_spans(CAT_OVER_ESCALATION, n=2)
    spans.extend(_make_spans(CAT_AGREEMENT, n=8))
    sigs = _collect_preflight_disagreement_signals(spans)
    assert sigs == []


def test_signal_sparse_silent_when_rate_below_sparse_threshold():
    # 15 decisions, 3 over-escalations = 20% rate. Count floor (3) is
    # met but rate (20%) is below sparse threshold (25%). Silent —
    # both conditions must hold.
    spans = _make_spans(CAT_OVER_ESCALATION, n=3)
    spans.extend(_make_spans(CAT_AGREEMENT, n=12))
    sigs = _collect_preflight_disagreement_signals(spans)
    assert sigs == []


def test_signal_normal_path_takes_priority_over_sparse():
    # 30+ decisions → high-confidence path. 16% rate clears the 15%
    # high-volume threshold and emits as "warn" — NOT as "info" with
    # the sparse qualifier. Pins that we don't accidentally double-fire
    # or downgrade severity when both tiers' conditions are met.
    spans = _make_spans(CAT_OVER_ESCALATION, n=16)
    spans.extend(_make_spans(CAT_AGREEMENT, n=84))
    sigs = _collect_preflight_disagreement_signals(spans)
    over = [s for s in sigs if s["type"] == "preflight_over_escalation"]
    assert len(over) == 1
    assert over[0]["severity"] == "warn"
    assert "small sample" not in over[0]["body"]
    assert over[0]["details"]["sparse_sample"] is False
    assert over[0]["details"]["threshold"] == 0.15


def test_signal_sparse_applies_to_all_three_categories():
    # Sanity: the sparse tier isn't just for over_escalation. Check
    # under_escalation fires too. (cascade_corrected requires multi-turn
    # session setup; covered by the bot-isolation test.)
    spans = _make_spans(CAT_UNDER_ESCALATION, n=4)
    spans.extend(_make_spans(CAT_AGREEMENT, n=4))
    sigs = _collect_preflight_disagreement_signals(spans)
    under = [s for s in sigs if s["type"] == "preflight_under_escalation"]
    assert len(under) == 1
    assert under[0]["severity"] == "info"
    assert under[0]["details"]["sparse_sample"] is True


def test_signal_under_escalation_fires_with_top_reasons():
    """The body should include the top firing reasons so the operator
    knows which rule is misrouting. This is the diagnostic data Phase 4
    RSI uses to propose pattern tweaks."""
    spans = []
    # 25 under_escalations from haiku:tier3
    for i in range(25):
        spans.append(_span(
            bot_id="b1", session_id=f"h-{i}",
            preflight_tier="tier3", tier_used="tier3",
            preflight_layer="haiku", preflight_reason="haiku:tier3",
            struggle_score=0.7, success=False,
        ))
    # 5 under_escalations from regex:factual_lookup
    for i in range(5):
        spans.append(_span(
            bot_id="b1", session_id=f"r-{i}",
            preflight_tier="tier3", tier_used="tier3",
            preflight_layer="regex", preflight_reason="regex:factual_lookup",
            struggle_score=0.7, success=False,
        ))
    # 70 agreements (so total decisions = 100, rate = 30%)
    for i in range(70):
        spans.append(_span(
            bot_id="b1", session_id=f"a-{i}",
            preflight_tier="tier3", tier_used="tier3",
            total_cost=0.005, success=True,
        ))
    sigs = _collect_preflight_disagreement_signals(spans)
    under = [s for s in sigs if s["type"] == "preflight_under_escalation"]
    assert len(under) == 1
    top_reasons = under[0]["details"]["top_reasons"]
    # Top-firing reason should be haiku:tier3 (25 events)
    assert top_reasons[0]["reason"] == "haiku:tier3"
    assert top_reasons[0]["count"] == 25


def test_signal_includes_actionable_fix_steps():
    spans = _make_spans(CAT_OVER_ESCALATION, n=30)
    spans.extend(_make_spans(CAT_AGREEMENT, n=70))
    sigs = _collect_preflight_disagreement_signals(spans)
    over = sigs[0]
    # fix_steps should name the per-bot opt-out path so an operator
    # can disable haiku for this bot without code changes
    assert "haiku_enabled = false" in over["fix_steps"]


def test_signal_per_bot_isolation():
    # bot_a: 30 over-escalations out of 100 → 30% (above 15%, fires)
    # bot_b: 3 over-escalations out of 30 → 10% (below 15%, silent)
    spans = _make_spans(CAT_OVER_ESCALATION, bot_id="bot_a", n=30)
    spans.extend(_make_spans(CAT_AGREEMENT, bot_id="bot_a", n=70))
    spans.extend(_make_spans(CAT_OVER_ESCALATION, bot_id="bot_b", n=3))
    spans.extend(_make_spans(CAT_AGREEMENT, bot_id="bot_b", n=27))
    sigs = _collect_preflight_disagreement_signals(spans)
    over = [s for s in sigs if s["type"] == "preflight_over_escalation"]
    # Only bot_a should fire
    assert len(over) == 1
    assert over[0]["details"]["bot_id"] == "bot_a"


def test_graded_categories_set_matches_constants():
    # Pin the GRADED_CATEGORIES set so a future "let's add a new category"
    # PR remembers to update both the constant and the rate-denominator
    # logic in compute_preflight_stats.
    assert GRADED_CATEGORIES == frozenset({
        CAT_AGREEMENT,
        CAT_OVER_ESCALATION,
        CAT_UNDER_ESCALATION,
        CAT_CASCADE_CORRECTED,
    })

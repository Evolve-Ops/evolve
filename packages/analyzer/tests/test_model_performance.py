"""Tests for model_performance.assemble_model_performance — the pod-wide,
model-centric PERFORMANCE rollup from cascade turn-spans (Phase 13 v2; spec
internal/spec-model-economics-page-2026-06-13.md §"v2 — performance").

Pins:
  1. latency p50/p95 (linear-interpolated, whole-turn ms).
  2. struggle_avg (mean over spans carrying a score).
  3. success_rate over the cascade.success-PRESENT subset (NOT all spans) + success_n.
  4. error_rate (error_info non-null ÷ spans).
  5. coverage_pct = span_count ÷ total_turns(identity).
  6. low_coverage threshold (< 20 spans, never hidden).
  7. identity normalization: qualified + bare spans collapse to ONE perf row that
     joins the cost row's (provider, model_id) identity (#2889-shared).
  8. empty / no-spans → graceful (no rows; pod coverage 0.0 or None).
  9. spans-but-no-cost-turns and cost-turns-but-no-spans both handled.

Placeholder model/provider names only (no real pod bot or provider names) so the
scrub guard stays green and no provider literal lands in the assertions.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from observability.opik_client import OpikSpan  # noqa: E402
import model_performance as mp  # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────────────

_BASE = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)


def _span(
    model,
    *,
    provider=None,
    duration_ms=1000,
    struggle=0.5,
    success=None,
    error=False,
    session_id="s0",
    turn_index=0,
) -> OpikSpan:
    """One cascade turn-span. duration_ms == end−start so latency_ms == duration_ms.

    ``struggle=None`` omits the score; ``success=None`` omits the field (absent,
    not present-False); ``error=True`` sets a non-empty error_info.
    """
    et = _BASE
    st = et - timedelta(milliseconds=duration_ms)
    attrs: dict = {"session_id": session_id, "turn_index": turn_index}
    if struggle is not None:
        attrs["cascade.struggle.score"] = struggle
    if success is not None:
        attrs["cascade.success"] = success
    return OpikSpan(
        name="bot_session_turn",
        start_time=st,
        end_time=et,
        type="general",
        model=model,
        provider=provider,
        producer="cascade_telemetry",
        error_info={"message": "boom"} if error else None,
        attributes=attrs,
    )


def _many(model, n, *, provider=None, **kw) -> list[OpikSpan]:
    return [_span(model, provider=provider, turn_index=i, **kw) for i in range(n)]


# ── 1. latency p50 / p95 ─────────────────────────────────────────────────────


def test_latency_percentiles_odd_sample():
    spans = [
        _span("provx/model-a", duration_ms=d)
        for d in (100, 200, 300, 400, 500)
    ]
    out = mp.assemble_model_performance(spans, {})
    row = out["by_identity"]["provx/model-a"]
    # n=5: p50 rank=2.0 → 300; p95 rank=3.8 → 400 + 0.8·100 = 480.
    assert row["latency_p50_ms"] == 300.0
    assert row["latency_p95_ms"] == 480.0


def test_latency_percentiles_even_sample_is_midpoint():
    spans = [_span("provx/model-a", duration_ms=d) for d in (100, 200, 300, 400)]
    out = mp.assemble_model_performance(spans, {})
    row = out["by_identity"]["provx/model-a"]
    # n=4: p50 rank=1.5 → midpoint(200,300)=250 (true median, not a biased pick).
    assert row["latency_p50_ms"] == 250.0


def test_latency_single_span_p50_equals_p95():
    out = mp.assemble_model_performance([_span("provx/model-a", duration_ms=777)], {})
    row = out["by_identity"]["provx/model-a"]
    assert row["latency_p50_ms"] == 777.0
    assert row["latency_p95_ms"] == 777.0


# ── 2. struggle_avg ──────────────────────────────────────────────────────────


def test_struggle_avg_mean_over_scored_spans():
    spans = [
        _span("provx/model-a", struggle=0.2),
        _span("provx/model-a", struggle=0.4),
        _span("provx/model-a", struggle=0.6),
    ]
    out = mp.assemble_model_performance(spans, {})
    assert out["by_identity"]["provx/model-a"]["struggle_avg"] == 0.4


def test_struggle_avg_none_when_no_scores():
    spans = [_span("provx/model-a", struggle=None)]
    out = mp.assemble_model_performance(spans, {})
    assert out["by_identity"]["provx/model-a"]["struggle_avg"] is None


# ── 3. success_rate over the PRESENT subset ──────────────────────────────────


def test_success_rate_denominator_is_present_subset_not_all_spans():
    # 4 spans: success present on 3 (True, False, True), absent on 1.
    spans = [
        _span("provx/model-a", success=True),
        _span("provx/model-a", success=False),
        _span("provx/model-a", success=None),   # absent — must NOT count
        _span("provx/model-a", success=True),
    ]
    out = mp.assemble_model_performance(spans, {})
    row = out["by_identity"]["provx/model-a"]
    # 2 successes / 3 present = 0.6667 — NOT 2/4 = 0.5.
    assert row["success_n"] == 3
    assert row["success_rate"] == round(2 / 3, 4)
    assert row["success_rate"] != 0.5


def test_success_rate_none_when_field_absent_everywhere():
    spans = [_span("provx/model-a", success=None), _span("provx/model-a", success=None)]
    out = mp.assemble_model_performance(spans, {})
    row = out["by_identity"]["provx/model-a"]
    assert row["success_rate"] is None
    assert row["success_n"] == 0


def test_success_field_must_be_real_bool_not_int():
    # A stray int 1 is NOT a present success — only genuine booleans count.
    spans = [_span("provx/model-a", success=1), _span("provx/model-a", success=True)]
    out = mp.assemble_model_performance(spans, {})
    row = out["by_identity"]["provx/model-a"]
    assert row["success_n"] == 1
    assert row["success_rate"] == 1.0


# ── 4. error_rate ────────────────────────────────────────────────────────────


def test_error_rate_is_errored_over_total():
    spans = [
        _span("provx/model-a", error=True),
        _span("provx/model-a", error=False),
        _span("provx/model-a", error=False),
        _span("provx/model-a", error=False),
    ]
    out = mp.assemble_model_performance(spans, {})
    assert out["by_identity"]["provx/model-a"]["error_rate"] == 0.25


# ── 5. coverage_pct vs total_turns ───────────────────────────────────────────


def test_coverage_pct_is_spans_over_total_turns_for_identity():
    spans = _many("provx/model-a", 10)
    total = {("provx", "model-a"): 40}
    out = mp.assemble_model_performance(spans, total)
    row = out["by_identity"]["provx/model-a"]
    assert row["span_count"] == 10
    assert row["coverage_pct"] == 0.25


def test_pod_perf_coverage_pct_uses_explicit_total_turns():
    spans = _many("provx/model-a", 5)
    out = mp.assemble_model_performance(spans, {("provx", "model-a"): 5}, total_turns=20)
    # pod coverage = total_spans(5) / pod total_turns(20) = 0.25, independent of
    # the per-identity denominators.
    assert out["total_spans"] == 5
    assert out["perf_coverage_pct"] == 0.25


def test_pod_perf_coverage_falls_back_to_sum_of_identity_turns():
    spans = _many("provx/model-a", 3)
    out = mp.assemble_model_performance(spans, {("provx", "model-a"): 12})
    # No explicit total_turns → denominator = sum of identity turns (12).
    assert out["perf_coverage_pct"] == round(3 / 12, 4)


# ── 6. low_coverage threshold ────────────────────────────────────────────────


def test_low_coverage_flag_below_default_threshold():
    out19 = mp.assemble_model_performance(_many("provx/model-a", 19), {})
    out20 = mp.assemble_model_performance(_many("provx/model-a", 20), {})
    assert out19["by_identity"]["provx/model-a"]["low_coverage"] is True
    assert out20["by_identity"]["provx/model-a"]["low_coverage"] is False


def test_low_coverage_never_hides_the_model():
    # A single thin-data span still produces a row (flagged), never dropped.
    out = mp.assemble_model_performance(_many("provx/model-a", 1), {})
    assert "provx/model-a" in out["by_identity"]
    assert out["by_identity"]["provx/model-a"]["low_coverage"] is True


def test_low_coverage_respects_min_spans_override():
    out = mp.assemble_model_performance(_many("provx/model-a", 5), {}, min_spans=5)
    assert out["by_identity"]["provx/model-a"]["low_coverage"] is False


# ── 7. identity normalization (qualified + bare collapse, joins cost row) ─────


def test_qualified_and_bare_spans_collapse_to_one_identity():
    spans = [
        _span("provx/model-a"),                       # qualified key
        _span("model-a", provider="provx"),           # bare key, resolved via map
        _span("model-a", provider=None),              # bare key, resolved via map
    ]
    out = mp.assemble_model_performance(
        spans, {("provx", "model-a"): 6}, bare_to_provider={"model-a": "provx"}
    )
    # All three land on ONE identity (the shared map resolves the bare twins) →
    # one row, span_count 3.
    assert list(out["by_identity"].keys()) == ["provx/model-a"]
    row = out["by_identity"]["provx/model-a"]
    assert row["span_count"] == 3
    assert row["provider"] == "provx"
    assert row["model_id"] == "model-a"
    # Joins the cost row's identity → coverage 3/6.
    assert row["coverage_pct"] == 0.5


def test_span_provider_field_never_overrides_cost_resolution():
    # JOIN-CORRECTNESS: a bare model key with a clean span `provider` field but NO
    # map entry must resolve to ("", bare) — EXACTLY what the cost layer keys it
    # (cost never sees the span provider). Honoring the span provider here would
    # key perf under ("provx", bare) and orphan it from the cost row's ("", bare).
    out = mp.assemble_model_performance([_span("model-q", provider="provx")], {})
    assert "model-q" in out["by_identity"]
    assert out["by_identity"]["model-q"]["provider"] == ""
    # And it joins the cost denominator keyed ("", "model-q").
    out2 = mp.assemble_model_performance(
        [_span("model-q", provider="provx")], {("", "model-q"): 4}
    )
    assert out2["by_identity"]["model-q"]["coverage_pct"] == 0.25


def test_perf_key_and_identity_match_cost_row_shape():
    # The by_identity key + provider/model_id mirror a cost row's `model` field
    # form so the route can join (provider.lower(), model_id) → perf.
    out = mp.assemble_model_performance([_span("provx/model-a")], {})
    entry = out["by_identity"]["provx/model-a"]
    assert entry["model"] == "provx/model-a"
    assert (entry["provider"].lower(), entry["model_id"]) == ("provx", "model-a")


def test_bare_span_without_provider_or_map_stays_unqualified():
    # No clean provider, no map entry → identity ("", bare), key is the bare id.
    out = mp.assemble_model_performance([_span("model-z", provider=None)], {})
    assert "model-z" in out["by_identity"]
    assert out["by_identity"]["model-z"]["provider"] == ""


# ── 8. empty / no-spans graceful ─────────────────────────────────────────────


def test_empty_spans_graceful():
    out = mp.assemble_model_performance([], {})
    assert out["by_identity"] == {}
    assert out["total_spans"] == 0
    # 0 turns known → pod coverage is None (no denominator), not a fake 0.
    assert out["perf_coverage_pct"] is None


def test_empty_spans_with_known_turns_is_zero_coverage():
    out = mp.assemble_model_performance([], {("provx", "model-a"): 50})
    assert out["by_identity"] == {}
    # 0 spans over 50 turns = honest 0.0 coverage.
    assert out["perf_coverage_pct"] == 0.0


# ── 9. spans-without-cost-turns and cost-turns-without-spans ──────────────────


def test_model_with_spans_but_no_cost_turns_reports_none_coverage():
    # Identity absent from total_turns_by_identity → coverage_pct None (can't
    # divide), but the perf metrics are still emitted.
    out = mp.assemble_model_performance(_many("provx/model-orphan", 4), {})
    row = out["by_identity"]["provx/model-orphan"]
    assert row["span_count"] == 4
    assert row["coverage_pct"] is None
    assert row["struggle_avg"] is not None


def test_cost_model_with_no_spans_absent_from_by_identity():
    # A model that has cost-turns but no spans simply does not appear — the route
    # then sets that row's perf to None.
    spans = _many("provx/model-a", 3)
    total = {("provx", "model-a"): 5, ("provx", "model-nospans"): 9}
    out = mp.assemble_model_performance(spans, total)
    assert "provx/model-a" in out["by_identity"]
    assert "provx/model-nospans" not in out["by_identity"]


# ── percentile helper unit pins ──────────────────────────────────────────────


def test_percentile_helper_edges():
    assert mp._percentile([], 50) is None
    assert mp._percentile([42.0], 95) == 42.0
    assert mp._percentile([0.0, 10.0], 50) == 5.0  # midpoint
    assert mp._percentile([0.0, 10.0], 95) == pytest.approx(9.5)

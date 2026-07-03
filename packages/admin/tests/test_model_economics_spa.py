"""Source-level pins for the Model Economics SPA page (Phase 13 v1.5).

The admin SPA has no JS test harness; the established pattern (see
test_ai_optimization_color_reorder.py) is to assert on the page module's
*source strings* + the index.html wiring. These pin that:

  1. The page is wired in: nav-item, page div, onPageActivate dispatch.
  2. The page fetches the endpoint and DEFAULT-sorts by $/turn desc.
  3. Provider chips reuse the AI-Optimization presentation map (_aiModelChip /
     _aiProviderForModel) — no provider literals in this file's logic.
  4. The confidence badge surfaces low_confidence as "insufficient data" and
     NEVER hides low-sample rows — in the bars they are DE-EMPHASIZED
     (.is-lowconf reduced opacity), not filtered out (spec §2).
  5. Configured-but-unused models render distinctly ("no usage yet").
  6. Honest-unit labels: $/turn is per-turn; effective $/1k is "incl. cache".
  7. Filter chips (provider/band/bot/audience), multi-select within a facet
     (the Reports/Alerts al-filter-chip pattern).
  8. New onclick handlers are window-exported (ESLint suppressions are
     baseline-shrink-only).
  9. v1.5 presentation: bars primary + metric switcher, table-toggle fallback,
     "Eff. cost/1k" rename, audience/bot grey-out of effective cost, and the
     band/role rollup strip.

Spec: docs/spec-model-economics-page-2026-06-13.md §"v1.5"
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_WEB = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"
_ME_JS = _WEB / "static" / "js" / "pages" / "model-economics.js"
_INDEX = _WEB / "index.html"


@pytest.fixture(scope="module")
def js() -> str:
    return _ME_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index() -> str:
    return _INDEX.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"(?m)//.*$", "", src)
    return src


# ── 1. Page wiring ───────────────────────────────────────────────────────────


def test_nav_item_present(index: str):
    assert 'data-page="model-economics"' in index
    assert "Model Economics" in index


def test_page_div_present(index: str):
    assert 'id="page-model-economics"' in index


def test_onpageactivate_dispatch(index: str):
    assert "if (page === 'model-economics') loadModelEconomics();" in index


def test_cross_linked_from_cost_and_ai_opt(index: str):
    # Both surfaces carry a prominent "Model Economics →" link.
    links = index.count("Model Economics →")
    assert links >= 2, f"expected cross-links from Cost + AI Optimization, found {links}"


# ── 2. Endpoint + default sort ───────────────────────────────────────────────


def test_fetches_model_economics_endpoint(js: str):
    assert "/api/analytics/model-economics" in js


def test_default_sort_is_cost_per_turn_desc(js: str):
    # Both the table sort and the default bar metric lead with $/turn desc.
    assert "_meSort = 'cost_per_turn'" in js
    assert "_meSortDir = 'desc'" in js
    assert "_meMetric = 'cost_per_turn'" in js


# ── 3. Provider-chip reuse (no provider literals) ────────────────────────────


def test_reuses_ai_optimization_provider_map(js: str):
    assert "_aiModelChip" in js
    assert "_aiProviderForModel" in js


def test_bar_hue_via_provider_class_not_literal(js: str):
    # Bars color by provider via the presentation class (→ --provider-color
    # custom prop), never a hardcoded provider→color map in JS logic.
    assert "_aiProviderColorClass" in js


def test_no_provider_literals_in_logic(js: str):
    code = _strip_comments(js).lower()
    for literal in ("anthropic", "openai", "google", "xai", "claude", "gpt-", "gemini", "grok"):
        assert literal not in code, f"provider literal {literal!r} leaked into model-economics.js logic"


# ── 4. Confidence — low-sample hidden by default, revealable + marked when shown ─


def test_confidence_badge_marks_low_sample(js: str):
    assert "insufficient data" in js
    assert "low_confidence" in js


def test_low_confidence_rows_hidden_by_default_revealable(js: str):
    # Low-sample ("insufficient data") rows are HIDDEN BY DEFAULT but revealable
    # via an explicit operator toggle (_meShowLowConf), and when shown they stay
    # MARKED (confidence badge) + de-emphasized (.is-lowconf). The pipeline drops
    # them only when the toggle is off — never a silent, un-revealable filter.
    code = _strip_comments(js)
    # Default OFF.
    assert "_meShowLowConf = false" in code, "the low-confidence toggle must default OFF"
    # _meViewRows drops low_confidence rows ONLY when the toggle is off.
    body = code[code.index("function _meViewRows"):]
    body = body[: body.index("return rows;") + len("return rows;")]
    assert "_meShowLowConf" in body and "low_confidence" in body, \
        "the pipeline must gate the low_confidence drop on the _meShowLowConf toggle"
    # When shown, low-sample rows are still marked + de-emphasized, not bare.
    assert "is-lowconf" in js, "shown low-confidence bars must be de-emphasized"
    assert "insufficient data" in js, "shown low-confidence rows must be marked"


# ── 5. Configured-but-unused ─────────────────────────────────────────────────


def test_unused_models_rendered_distinctly(js: str):
    assert "_meRenderUnused" in js
    assert "no usage yet" in js
    assert ".unused" in js or "_meData.unused" in js


# ── 6. Honest-unit labels ────────────────────────────────────────────────────


def test_per_turn_labeled_honestly(index: str):
    # The leaderboard card explains $/turn is per-turn (sub-calls fold in).
    assert "per-turn" in index or "sub-calls fold" in index


def test_effective_per_1k_labeled_incl_cache(js: str, index: str):
    blob = js + index
    assert "incl. cache" in blob


# ── 7. Filter chips — provider / band / bot / audience, multi-select ─────────


def test_filter_chips_multi_facet(js: str):
    # Reports/Alerts al-filter-chip pattern: a toggle + clear, four facets.
    assert "_meToggleChip" in js
    assert "_meClearChips" in js
    for facet in ("'provider'", "'band'", "'bot'", "'audience'"):
        assert facet in js, f"facet {facet} missing from the chip logic"


def test_filter_controls_in_page(index: str):
    # The chip strip + view/metric/rollup controls are wired into the page.
    assert 'id="me-filters"' in index
    assert 'id="me-view-toggle"' in index
    assert 'id="me-metric-switch"' in index


# ── 8. window-exported handlers ──────────────────────────────────────────────


def test_onclick_handlers_window_exported(js: str):
    for fn in ("loadModelEconomics", "_meSetDays", "_meSetView", "_meSetMetric",
               "_meSetRollupAxis", "_meToggleChip", "_meClearChips",
               "_meSortBy", "_meToggleRow"):
        assert f"window.{fn} = {fn};" in js, f"{fn} not window-exported"


# ── 9. v1.5 presentation — bars / metric switcher / table toggle / rollup ────


def test_default_view_is_bars(js: str):
    assert "_meView = 'bars'" in js
    assert "_meRenderBars" in js


def test_metric_switcher_five_metrics(js: str):
    # Each switcher metric maps to a row field, with the spec's labels.
    for field in ("cost_per_turn", "usd_per_1k_blended", "total_cost",
                  "share_of_spend", "turns"):
        assert field in js, f"metric field {field} missing"
    for label in ("$/turn", "Eff. cost/1k", "Spend", "Share", "Turns"):
        assert label in js, f"metric label {label!r} missing"


def test_eff_cost_rename_applied(js: str):
    # "EFF $/1k" reads as effectiveness; v1.5 renames it to effective COST.
    assert "Eff. cost/1k" in js
    assert "Eff $/1k" not in js
    assert "EFF $/1k" not in js


def test_table_view_preserved(js: str, index: str):
    # The v1 dense multi-stat table is kept behind the Bars|Table toggle.
    assert "_meRenderTable" in js
    assert "_meSetView" in js
    assert 'id="me-bars"' in index
    assert 'id="me-table"' in index


def test_audience_greys_effective_cost(js: str):
    # Eff. cost/1k is unavailable per-audience and per-bot (no per-slice
    # tokens). The eff metric is gated on _meEffUnavailable() (disabled +
    # auto-downgraded), and both re-scope paths NULL the effective-cost
    # fields rather than show a stale/wrong number.
    assert "_meEffUnavailable" in js
    assert "m.eff && effOff" in js
    assert js.count("usd_per_1k_blended = null") >= 2


def test_rollup_strip_band_and_role(js: str, index: str):
    assert "_meSetRollupAxis" in js
    assert "by_band" in js
    assert "by_role" in js
    assert "_meSetRollupAxis('band')" in index
    assert "_meSetRollupAxis('role')" in index


# ── style-guide: expand uses .expand-icon, not unicode triangles ─────────────


def test_expand_uses_expand_icon_class(js: str, index: str):
    blob = js + index
    assert "expand-icon" in blob


# ── 10. v2 performance UI (Bite B) — spec §"v2 — performance" → §Presentation ─


def test_perf_metrics_in_switcher(js: str):
    # The five perf metrics join the metric switcher as additional bar metrics.
    for field in ("struggle_avg", "latency_p50_ms", "latency_p95_ms",
                  "success_rate", "error_rate"):
        assert field in js, f"perf metric field {field} missing from the switcher"
    for label in ("Struggle", "Latency p50", "Latency p95", "Success", "Error rate"):
        assert label in js, f"perf metric label {label!r} missing"


def test_perf_metrics_read_from_perf_block(js: str):
    # Perf metrics live under row.perf (span subset) — NEVER conflated with the
    # top-level all-turns cost fields. _meMetricValue routes perf keys to r.perf.
    assert "_meMetricValue" in js
    code = _strip_comments(js)
    body = code[code.index("function _meMetricValue"):]
    body = body[: body.index("}") + 1]
    assert "r.perf" in body, "_meMetricValue must read perf metrics off row.perf"


def test_struggle_direction_lower_is_smoother(js: str):
    # struggle_avg: LOWER = smoother. The UI must say so (don't let a long bar
    # read as 'good'). The metric carries good:'low' and a 'lower = smoother' note.
    assert "lower = smoother" in js
    assert "good: 'low'" in js


def test_null_perf_renders_no_span_data(js: str):
    # row.perf === null → "no span data" (the model is NEVER hidden, just marked).
    assert "_mePerfBadge" in js
    assert "no span data" in js
    code = _strip_comments(js)
    badge = code[code.index("function _mePerfBadge"):]
    badge = badge[: badge.index("\n}\n") + 1] if "\n}\n" in badge else badge[:600]
    assert "no span data" in badge, "_mePerfBadge must emit 'no span data' for null perf"


def test_low_coverage_badge(js: str):
    # low_coverage (span_count < 20) → "insufficient span data" over thin figures.
    assert "low_coverage" in js
    assert "insufficient span data" in js


def test_per_model_coverage_pct_shown(js: str):
    # coverage_pct surfaced per model where present.
    assert "coverage_pct" in js


def test_pod_level_perf_coverage_in_header(js: str, index: str):
    # perf_coverage_pct + perf_total_spans surfaced near the leaderboard header.
    assert "_meRenderPerfCoverage" in js
    assert "perf_coverage_pct" in js
    assert "perf_total_spans" in js
    assert 'id="me-perf-coverage"' in index


def test_perf_table_columns(js: str):
    # The same perf metrics render as columns + a per-model coverage badge column.
    assert "_mePerfCell" in js
    for label in ("Struggle", "Lat p50", "Lat p95", "Success", "Errors", "Perf cov."):
        assert label in js, f"perf table column {label!r} missing"


def test_perf_is_all_audience_labeled(js: str):
    # Perf has NO audience leg — under ?audience=human|auto it stays all-audience.
    # The UI must label it so, never imply the perf numbers are audience-scoped.
    assert "all-audience" in js
    # The note recasts ONLY cost to the filter and keeps perf all-audience.
    assert "perf stays all-audience" in js


def test_perf_not_conflated_with_cost(js: str):
    # Honesty invariant: cost is over ALL turns, perf over the SUBSET — said so.
    assert "span-covered subset" in js or "span subset" in js
    # latency is whole-turn wall-clock, NOT pure inference — labeled honestly.
    assert "whole-turn" in js


# ── 11. used-but-low-volume legibility (model-tiers: "Opus is missing" fix) ──
# A used-but-low-volume model (< 10k billed tokens) is kept out of the confident
# ranking but NEVER silently hidden: an always-visible "+N below the confidence
# line" affordance NAMES + counts the hidden models and reveals them in place,
# and the rollup card reads "insufficient data" (used_count) instead of "0
# models" when a band/role's members are all low-volume.


def test_lowconf_affordance_names_and_counts(js: str):
    # The always-visible affordance shows the COUNT and NAMES the hidden models
    # (first ~3 + "and N more"), backed by the _meShowLowConf reveal state — so a
    # used model can never silently vanish.
    assert "_meLowConfAffordance" in js
    assert "_meLowConfNames" in js
    assert "below the 10k-token confidence threshold" in js
    assert "and ${more} more" in js, "affordance must name first ~3 + 'and N more'"
    # Wired to the reveal state (replaces the old global checkbox), togglable.
    assert "_meToggleLowConf" in js
    assert "_meShowLowConf" in js


def test_lowconf_affordance_uses_expand_icon_not_unicode(js: str):
    # Disclosure uses the .expand-icon SVG chevron (style-guide §9.13), inside the
    # affordance body — not a unicode triangle.
    code = _strip_comments(js)
    body = code[code.index("function _meLowConfAffordance"):]
    body = body[: body.index("\n}") + 2]
    assert "expand-icon" in body
    for glyph in ("▸", "▾", "▼", "▶", "⟩"):
        assert glyph not in body, f"unicode triangle {glyph!r} in the affordance"


def test_confident_ranking_kept_clean_lowconf_below(js: str):
    # Confident rows render first (the clean $/turn ranking); low-conf rows are a
    # SEPARATE group rendered below the affordance, so they never jump the sort.
    assert "confident" in js and "lowconf" in js
    code = _strip_comments(js)
    # The bars compose confident bars, then the affordance, then low-conf bars.
    assert "confidentBars" in code and "lowconfBars" in code


def test_rollup_card_used_count_insufficient_data(js: str):
    # A band/role whose members are all low-volume reads "insufficient data"
    # (driven by used_count), not the misleading "0 models · 0 turns".
    assert "used_count" in js
    assert "insufficient data" in js


def test_headline_reconciles_used_vs_confident(js: str):
    # The "Models in use" stat annotates the used-vs-confident split so the count
    # reconciles with the +N affordance instead of reading as an unexplained gap.
    assert "confident cost data" in js
    assert "low-volume" in js

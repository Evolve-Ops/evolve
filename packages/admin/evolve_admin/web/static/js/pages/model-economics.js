// ════════════════════════════════════════════════════════════════════════
// Page: Model Economics (pod-wide, model-centric cost leaderboard) — v1.5
//
// The TRANSPOSE of the bot-centric Usage / Cost page: one row per model
// IDENTITY used across the WHOLE pod (sub-series already collapsed by the
// Bite A data layer — no duplicate opus-4-8). The "Models lens" off AI
// Optimization — same data the Cost page consumes (by_model /
// by_model_by_audience), assembled model-first so an operator tuning model
// choice can compare within a band / across providers.
//
// v1.5 presentation (spec §"v1.5", internal/spec-model-economics-page-2026-06-13.md):
//   1. Bars primary + table toggle. Default = horizontal bar chart, one bar
//      per model, sorted desc by the selected metric (metric switcher:
//      $/turn · Eff. cost/1k · spend · share · turns). A toggle flips to the
//      v1 dense multi-stat table (kept verbatim as the fallback view).
//   2. Low-confidence rows render dimmed in the bars (never hidden; the
//      confidence badge stays).
//   3. "Eff. cost/1k" label (effective COST incl. cache — NOT effectiveness).
//   4. Filter chips (Reports/Alerts al-filter-chip pattern): provider · band ·
//      bot · audience. Multi-select within a facet, AND across facets.
//   5. Aggregate rollup strip (band ↔ role toggle), blended $/turn per group.
//   6. Tier column = the pricing cost-band; configured / off-catalog /
//      unexpected-billing are badges, not rows.
//
// Honest units (spec §Invariants):
//   - $/turn is PER-TURN, not per-LLM-call (sub-calls fold into the parent
//     turn) — labeled as such.
//   - Effective $/1k is incl-cache (the #2797 contract) — labeled "incl. cache
//     cost", never relabeled as a clean per-token rate. UNAVAILABLE per-audience
//     and per-bot (no per-slice token volume) → the metric is greyed/nulled
//     when an audience or bot filter is active.
//   - low_confidence ("insufficient data") rows are kept out of the confident
//     ranking by default (a sub-10k-token $/turn isn't authoritative), but they
//     are NEVER silently dropped: an always-visible "+N used models below the
//     10k-token confidence threshold — <names> — show" affordance sits at the
//     bottom of the bars AND the table whenever ≥1 used model is low-confidence.
//     It NAMES the hidden models (so e.g. a low-volume power-rung model is
//     visible by name even while collapsed) and expands them in place (dimmed via
//     .is-lowconf + the "insufficient data" badge), backed by _meShowLowConf.
//     The confident ranking above stays clean — a low-conf row never jumps the
//     $/turn sort.
//
// Filtering is CLIENT-SIDE over one pod-wide fetch (the Reports/Alerts chip
// pattern): provider/band drop rows; audience re-splits from each row's
// by_model_by_audience legs; bot re-scopes from the (bot × model) matrix the
// Bite A layer ships for exactly this. Bot and audience are mutually exclusive
// facets — the per-bot matrix carries no audience split and the audience legs
// are pod-wide, so the two can't compose exactly; the UI disables one while the
// other is active rather than show a wrong number.
//
// Reuses the AI-Optimization provider presentation map (_aiModelChip /
// _aiProviderForModel / _aiProviderColorClass / _aiProviderLabel) — NO provider
// literals in this file's logic. Reuses cost.js's _usageFmt$.
//
// Backed by GET /api/analytics/model-economics. Dispatched via
// onPageActivate('model-economics').
// ════════════════════════════════════════════════════════════════════════

// State
let _meDays = 30;          // pod-wide lens → wider default window than Cost's 7
let _meData = null;        // last pod-wide payload (filtering is client-side)
let _meBotList = [];       // distinct bot ids (from the pod-wide matrix), cached
let _meView = 'bars';      // 'bars' | 'table'
let _meMetric = 'cost_per_turn';   // bar metric (metric switcher)
let _meRollupAxis = 'band';        // 'band' | 'role'
let _meShowLowConf = false;        // show low-confidence ("insufficient data") rows — default OFF
let _meFilters = { provider: new Set(), band: new Set(), bot: new Set(), audience: new Set() };
// Table-only sort state (the v1 table is the fallback view — kept).
let _meSort = 'cost_per_turn';
let _meSortDir = 'desc';
let _meExpanded = {};      // model key → expanded (in/out detail) in the table

// Canonical cost bands (cost order) — ordering DATA, not provider literals.
const _ME_BANDS = ['low', 'medium', 'high', 'premium'];

// Metric switcher definitions. `eff` marks the effective-cost metric, which is
// unavailable when an audience or bot filter is active (no per-slice tokens).
//
// `perf: true` metrics (v2) read from the per-row `perf` block (NOT the top-level
// cost fields) — they live over the span-covered SUBSET (~49% of turns), are
// ALWAYS all-audience (spans carry no audience leg), and must never be conflated
// with the all-turns cost figures. `good` records the better direction so the UI
// can warn that a longer bar is not necessarily "good" (struggle / latency /
// errors: lower is better). `note` is the per-metric direction caption.
//
// The two latency perf-field names are pulled out as named constants (they must
// equal the route's `perf` block field names). Referencing them via identifiers
// instead of inline string literals also keeps gitleaks' generic-api-key
// heuristic from false-positiving on a digit-bearing token sitting next to the
// `key` property — a real flagged false positive, and named constants for a
// field name reused in 4+ places is the cleaner shape regardless.
const _ME_LAT_P50 = 'latency_p50_ms';
const _ME_LAT_P95 = 'latency_p95_ms';

const _ME_METRICS = [
  { key: 'cost_per_turn',      label: '$/turn',       eff: false, group: 'cost' },
  { key: 'usd_per_1k_blended', label: 'Eff. cost/1k', eff: true,  group: 'cost' },
  { key: 'total_cost',         label: 'Spend',        eff: false, group: 'cost' },
  { key: 'share_of_spend',     label: 'Share',        eff: false, group: 'cost' },
  { key: 'turns',              label: 'Turns',        eff: false, group: 'cost' },
  { key: 'struggle_avg',       label: 'Struggle',      perf: true, group: 'perf', good: 'low',  note: 'lower = smoother — a longer bar means MORE struggle' },
  { key: _ME_LAT_P50,          label: 'Latency p50',   perf: true, group: 'perf', good: 'low',  note: 'lower = faster — a longer bar means a SLOWER turn (whole-turn wall-clock, incl. tools/waits)' },
  { key: _ME_LAT_P95,          label: 'Latency p95',   perf: true, group: 'perf', good: 'low',  note: 'lower = faster — a longer bar means a SLOWER turn (whole-turn wall-clock, incl. tools/waits)' },
  { key: 'success_rate',       label: 'Success',       perf: true, group: 'perf', good: 'high', note: 'higher = better (over the spans that carried a success field)' },
  { key: 'error_rate',         label: 'Error rate',    perf: true, group: 'perf', good: 'low',  note: 'lower = fewer errors — a longer bar means MORE errors' },
];

function _meMetricDef(key) { return _ME_METRICS.find(x => x.key === key); }

// Read a metric value off a row. Perf metrics live under `row.perf` (the
// span-subset block); a model with no spans has `row.perf === null` → null
// value (sorts last, renders the "no span data" badge). Cost metrics read the
// top-level row fields.
function _meMetricValue(r, key) {
  const m = _meMetricDef(key);
  if (m && m.perf) return r.perf ? r.perf[key] : null;
  return r[key];
}

// ── Formatting helpers (small, local; $/turn + $/1k both want 3-4 sig figs) ──

function _meFmtPerTurn(v) {
  if (v == null || v <= 0) return '—';
  // $/turn spans $0.0001 (cheap fast model) to $1+ (a power-model turn) — show
  // enough precision to compare without a wall of zeros.
  if (v >= 0.1) return '$' + v.toFixed(3);
  if (v >= 0.001) return '$' + v.toFixed(4);
  return '$' + v.toFixed(5);
}

function _meFmtPer1k(v) {
  if (v == null || v <= 0) return '—';
  if (v >= 1) return '$' + v.toFixed(2);
  return '$' + v.toFixed(3);
}

function _meFmtPct(v) {
  if (v == null) return '—';
  return (v * 100).toFixed(1) + '%';
}

// Perf formatters (over the span-covered subset).
// struggle_avg ∈ [0,1] — a score, NOT a rate; render as a 2-decimal value so it
// reads distinctly from the success/error rates (and never as a clean percentage).
function _meFmtScore(v) {
  if (v == null) return '—';
  return Number(v).toFixed(2);
}
// success_rate / error_rate ∈ [0,1] — genuine rates → percent.
function _meFmtRate(v) {
  if (v == null) return '—';
  return (v * 100).toFixed(0) + '%';
}
// latency p50/p95 — whole-turn wall-clock in ms (incl. tools/waits, NOT pure
// inference). Roll up to seconds once it stops fitting in a tidy ms figure.
function _meFmtMs(v) {
  if (v == null || v < 0) return '—';
  if (v >= 10000) return (v / 1000).toFixed(0) + 's';
  if (v >= 1000) return (v / 1000).toFixed(1) + 's';
  return Math.round(v) + 'ms';
}

function _meFmtMetric(key, v) {
  if (key === 'cost_per_turn') return _meFmtPerTurn(v);
  if (key === 'usd_per_1k_blended') return _meFmtPer1k(v);
  if (key === 'total_cost') return _usageFmt$(v);
  if (key === 'share_of_spend') return _meFmtPct(v);
  if (key === 'turns') return (v || 0).toLocaleString();
  if (key === 'struggle_avg') return _meFmtScore(v);
  if (key === _ME_LAT_P50 || key === _ME_LAT_P95) return _meFmtMs(v);
  if (key === 'success_rate' || key === 'error_rate') return _meFmtRate(v);
  return v == null ? '—' : String(v);
}

function _meMetricLabel(key) {
  const m = _ME_METRICS.find(x => x.key === key);
  return m ? m.label : key;
}

function _meFmtDelta(v) {
  // eff-vs-list on the blended $/1k axis. Negative = cheaper than list
  // midpoint (cache savings); positive = running over. Colored semantically:
  // savings (negative) green, overage (positive) yellow.
  if (v == null) return '<span style="color:var(--text3)">—</span>';
  const color = v < 0 ? 'var(--green)' : (v > 0 ? 'var(--yellow)' : 'var(--text2)');
  // _meFmtPer1k(|v|) renders "$0.045"; prefix the sign onto the dollar amount.
  const mag = _meFmtPer1k(Math.abs(v));
  const signed = v < 0 ? '−' + mag : (v > 0 ? '+' + mag : mag);
  return `<span style="color:${color}">${signed}</span>`;
}

function _meRecency(ts) {
  if (!ts) return '—';
  const d = new Date(ts);
  if (isNaN(d.getTime())) return '—';
  const days = Math.floor((Date.now() - d.getTime()) / 86400000);
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  return days + 'd ago';
}

// Provider key for a row — the row's own provider, else derived from the model
// id via the presentation map. NO provider literal here.
function _meRowProvider(r) {
  return r.provider || (typeof _aiProviderForModel === 'function'
    ? _aiProviderForModel(r.model) : '') || '';
}

// Provider-colored model chip. Reuses the AI-Optimization presentation map so
// the hue matches the tier editor and there's NO provider literal here.
// Falls back gracefully if ai-optimization.js isn't loaded (defensive — the
// SPA loads all page modules before any page activates, so this is belt-and-
// braces only).
function _meModelChip(model, provider) {
  const short = String(model || '').replace(':unexpected_billing', '').split('/').pop();
  if (typeof _aiModelChip === 'function') {
    const prov = provider || (typeof _aiProviderForModel === 'function'
      ? _aiProviderForModel(model) : '');
    return _aiModelChip(short, prov);
  }
  return `<span class="ai-model-provider-chip ai-provider-unknown">${escHtml(short)}</span>`;
}

// Resolve the provider→color CSS class for a row, so a bar fill can carry the
// provider hue (via the --provider-color custom prop the class sets). Falls
// back to the neutral "unknown" class.
function _meProviderClass(r) {
  if (typeof _aiProviderColorClass === 'function') return _aiProviderColorClass(_meRowProvider(r));
  return 'ai-provider-unknown';
}

// Confidence badge. low_confidence (billed_tokens < 10k min-sample gate, owned
// by usage_analytics) → "insufficient data". Otherwise the sample size as a
// reassuring "ok" badge. NEVER hides the row — marks it (spec §Invariant 2).
function _meConfidenceBadge(row) {
  // billed_tokens is nulled when an audience/bot filter re-scopes the row (no
  // per-slice token volume) — fall back to a neutral "scoped" marker then.
  if (row.billed_tokens == null) {
    return '<span class="badge badge-sm badge-neutral" title="Sample size is unavailable when an audience or bot filter is active (no per-slice token volume).">scoped</span>';
  }
  const tok = (row.billed_tokens || 0).toLocaleString();
  if (row.low_confidence) {
    return `<span class="badge badge-sm badge-warn" title="Only ${tok} billed tokens — below the 10k min-sample gate. The $/turn and $/1k figures are shown but not authoritative.">insufficient data</span>`;
  }
  return `<span class="badge badge-sm badge-ok" title="${tok} billed tokens — above the 10k min-sample gate.">${tok} tok</span>`;
}

// Per-model span-coverage badge for the PERF figures (spec v2 §"Coverage gate").
// Cost is over ALL turns; perf is over the span-covered SUBSET — every perf
// figure carries this badge so the two are never conflated. Three states:
//   - row.perf === null            → "no span data" (the model has zero spans;
//                                     never hide it, just mark the perf as absent)
//   - perf.low_coverage === true   → "insufficient span data" (< 20 spans) over
//                                     the thin figures
//   - otherwise                    → span count + coverage_pct where present
// `coverage_pct` is null when the model has spans but no cost-turns (no
// denominator) — render the count without a fabricated percentage.
function _meCoveragePctText(p) {
  return p.coverage_pct == null ? '' : ` · ${(p.coverage_pct * 100).toFixed(0)}% of turns`;
}
function _mePerfBadge(row) {
  const p = row.perf;
  if (!p) {
    return '<span class="badge badge-sm badge-neutral" title="No cascade spans recorded for this model in this window — performance is unavailable (cost is still over ALL turns).">no span data</span>';
  }
  const n = p.span_count || 0;
  const cov = _meCoveragePctText(p);
  if (p.low_coverage) {
    return `<span class="badge badge-sm badge-warn" title="Only ${n} span${n === 1 ? '' : 's'}${cov} — below the 20-span min. Performance is shown but not authoritative.">insufficient span data</span>`;
  }
  return `<span class="badge badge-sm badge-ok" title="${n} span${n === 1 ? '' : 's'}${cov}. Performance is over the span-covered subset (all-audience), distinct from the all-turns cost figures.">${n} span${n === 1 ? '' : 's'}</span>`;
}

// Cost-band badge (neutral — band is a category, not a health state).
function _meBandBadge(band) {
  if (!band) return '<span class="badge badge-sm badge-neutral" title="No cost band resolved (no list price, no trustworthy observed cost, no family match).">unbanded</span>';
  return `<span class="badge badge-sm badge-neutral">${escHtml(band)}</span>`;
}

// ── Filtering pipeline (client-side over one pod-wide payload) ───────────────

// Active audience leg, or null when the facet is off / both legs selected
// (human + auto == all). 'auto' maps to the non_human leg.
function _meAudienceSel() {
  const s = _meFilters.audience;
  if (s.size === 0 || s.size === 2) return null;
  return s.has('human') ? 'human' : 'non_human';
}

function _meBotActive() { return _meFilters.bot.size > 0; }

// Effective-cost is unavailable (must grey) when an audience or bot filter is
// active — neither slice carries per-slice token volume.
function _meEffUnavailable() { return _meBotActive() || _meAudienceSel() != null; }

// The metric actually rendered: downgrade eff-cost to $/turn when unavailable.
function _meActiveMetric() {
  const m = _ME_METRICS.find(x => x.key === _meMetric);
  if (m && m.eff && _meEffUnavailable()) return 'cost_per_turn';
  return _meMetric;
}

// Re-scope rows to selected bot(s) using the (bot × model) matrix. Sums
// cost/calls across the chosen bots per model identity; recomputes $/turn.
// Effective-cost fields are nulled — the matrix has no token volume.
function _meApplyBotScope(rows, botSet) {
  const matrix = (_meData && _meData.bot_model_matrix) || [];
  const agg = new Map();   // "provider/model_id" → {cost, calls}
  for (const m of matrix) {
    if (!botSet.has(m.bot_id)) continue;
    const k = String(m.provider || '').toLowerCase() + '\u0000' + (m.model_id || '');
    const cur = agg.get(k) || { cost: 0, calls: 0 };
    cur.cost += (m.cost || 0);
    cur.calls += (m.calls || 0);
    agg.set(k, cur);
  }
  const out = [];
  for (const r of rows) {
    const k = String(r.provider || '').toLowerCase() + '\u0000' + (r.model_id || '');
    const a = agg.get(k);
    if (!a || a.calls <= 0) continue;
    const rr = Object.assign({}, r);
    rr.turns = a.calls;
    rr.total_cost = a.cost;
    rr.cost_per_turn = a.calls > 0 ? a.cost / a.calls : null;
    rr.usd_per_1k_blended = null; rr.usd_per_1k_input = null; rr.usd_per_1k_output = null;
    rr.eff_vs_list_delta = null; rr.billed_tokens = null;
    out.push(rr);
  }
  return out;
}

// Re-cast rows to a single audience leg using each row's by_model_by_audience
// {calls, cost}. Mirrors the server _apply_audience_view: the four audience-safe
// metrics recompute; effective-cost is nulled (no per-audience token volume).
function _meApplyAudienceScope(rows, aud) {
  const out = [];
  for (const r of rows) {
    const leg = (r.audience && r.audience[aud]) || { calls: 0, cost: 0 };
    const calls = leg.calls || 0, cost = leg.cost || 0;
    if (calls <= 0) continue;
    const rr = Object.assign({}, r);
    rr.turns = calls;
    rr.total_cost = cost;
    rr.cost_per_turn = calls > 0 ? cost / calls : null;
    rr.usd_per_1k_blended = null; rr.usd_per_1k_input = null; rr.usd_per_1k_output = null;
    rr.eff_vs_list_delta = null; rr.billed_tokens = null;
    out.push(rr);
  }
  return out;
}

// The FULL filtered row set for the current chip selection — provider/band are
// plain row filters (OR within a facet), bot XOR audience re-scopes the metrics.
// Share-of-spend is recomputed over the filtered total (INCL. low-confidence
// rows — they are real, if low-volume, spend) so it stays stable whether or not
// the low-conf rows are revealed. The low-confidence DROP is NOT applied here:
// the caller separates confident (the clean ranking) from low-conf (surfaced
// below the "+N below the confidence line" affordance), so a low-conf row never
// jumps the $/turn sort.
function _meFilteredRows() {
  let rows = (_meData && _meData.rows ? _meData.rows : []).map(r => Object.assign({}, r));
  if (_meFilters.provider.size) rows = rows.filter(r => _meFilters.provider.has(_meRowProvider(r)));
  if (_meFilters.band.size) rows = rows.filter(r => _meFilters.band.has(r.band || ''));

  if (_meBotActive()) {
    rows = _meApplyBotScope(rows, _meFilters.bot);
  } else {
    const aud = _meAudienceSel();
    if (aud) rows = _meApplyAudienceScope(rows, aud);
  }

  const tot = rows.reduce((a, r) => a + (r.total_cost || 0), 0);
  rows.forEach(r => { r.share_of_spend = tot > 0 ? (r.total_cost || 0) / tot : null; });
  return rows;
}

// Rows for the main confident ranking plus — when the operator has revealed them
// via the affordance — the low-confidence rows. The low-conf drop is gated on the
// _meShowLowConf toggle, NEVER a silent filter: when the toggle is off the
// low-conf rows are surfaced (named + counted) by the "+N below the confidence
// line" affordance, not erased. (Bars/table split _meFilteredRows() into the two
// groups directly so the confident $/turn ranking stays clean; this composed list
// backs the filtered-count note.)
function _meViewRows() {
  let rows = _meFilteredRows();
  if (!_meShowLowConf) rows = rows.filter(r => !r.low_confidence);
  return rows;
}

// Short, comma-joined model names for the affordance line: first ~3 + "and N
// more". Names (not just a count) so a used model — e.g. a low-volume power-rung
// model — is visible by name even while its row is collapsed below the line.
function _meLowConfNames(rows) {
  const names = rows.map(r => String(r.model || r.model_id || '')
    .replace(':unexpected_billing', '').split('/').pop());
  const shown = names.slice(0, 3);
  const more = names.length - shown.length;
  return shown.join(', ') + (more > 0 ? `, and ${more} more` : '');
}

// The always-visible "below the confidence line" affordance. Rendered at the
// bottom of the bars list AND the table whenever ≥1 used model is low-confidence
// (< 10k billed tokens). It NAMES the hidden models so nothing used silently
// vanishes; clicking expands them in place (dimmed + the "insufficient data"
// badge), backed by the _meShowLowConf reveal state. Uses the .expand-icon SVG
// chevron (style-guide §9.13) — not a unicode triangle.
function _meLowConfAffordance(rows) {
  if (!rows.length) return '';
  const n = rows.length;
  const open = _meShowLowConf;
  const icon = `<span class="expand-icon${open ? ' is-open' : ''}" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>`;
  const action = open ? 'hide' : 'show';
  const tip = `These ${n} model${n === 1 ? '' : 's'} ran on the pod but have under 10k billed tokens, so their $/turn isn't statistically authoritative. They are kept out of the confident ranking above; click to ${action} them (dimmed, flagged "insufficient data").`;
  return `<button class="me-lowconf-line" onclick="_meToggleLowConf(${open ? 'false' : 'true'})" aria-expanded="${open}" title="${escHtml(tip)}">
    ${icon}<span class="me-lowconf-line-text">+${n} used model${n === 1 ? '' : 's'} below the 10k-token confidence threshold — ${escHtml(_meLowConfNames(rows))}</span>
    <span class="me-lowconf-line-action">${action}</span>
  </button>`;
}

function _meAnyFilter() {
  return _meFilters.provider.size || _meFilters.band.size
    || _meFilters.bot.size || _meFilters.audience.size;
}

// ── Loader ───────────────────────────────────────────────────────────────────

async function loadModelEconomics() {
  const barsEl = document.getElementById('me-bars');
  if (barsEl) barsEl.innerHTML = '<div class="empty" style="padding:14px">Loading…</div>';
  // Pod-wide fetch (no server-side facet params — filtering is client-side over
  // the full payload, matching the Reports/Alerts chip pattern).
  const res = await api('GET', `/api/analytics/model-economics?days=${encodeURIComponent(_meDays)}`);
  if (!res || res.error) {
    if (barsEl) barsEl.innerHTML = `<div class="error" style="padding:10px;font-size:0.82rem">${escHtml((res && res.error) || 'failed to load model economics')}</div>`;
    return;
  }
  _meData = res;
  // Cache the distinct bot list from the pod-wide matrix for the bot chips.
  const bots = new Set();
  for (const m of (res.bot_model_matrix || [])) { if (m.bot_id) bots.add(m.bot_id); }
  _meBotList = [...bots].sort();
  _meRenderHeadline();
  _meRenderPerfCoverage();
  _meRenderRollup();
  _meRenderFilters();
  _meRenderMetricSwitch();
  _meRenderActiveView();
  _meRenderUnused();
}

// Pod-level performance coverage, surfaced by the leaderboard header so the
// partial (~49%) span coverage is visible UP FRONT — the operator must see that
// perf is over a subset before reading any per-model perf figure. Cost is over
// ALL turns; this is the perf denominator gap made legible (spec v2 §"Coverage
// gate"). `perf_coverage_pct` is null when the pod logged 0 turns.
function _meRenderPerfCoverage() {
  const el = document.getElementById('me-perf-coverage');
  if (!el || !_meData) return;
  const pct = _meData.perf_coverage_pct;
  const spans = _meData.perf_total_spans || 0;
  if (pct == null && !spans) {
    el.innerHTML = '<span class="badge badge-sm badge-neutral" title="No cascade spans in this window — performance figures are unavailable. Cost figures are still over ALL turns.">no span data</span>';
    return;
  }
  const pctTxt = pct == null ? '—' : (pct * 100).toFixed(0) + '%';
  el.innerHTML = `<span class="badge badge-sm badge-neutral" title="Performance is read from cascade turn-spans, which cover ${pctTxt} of pod turns (${spans.toLocaleString()} spans). Cost is over ALL turns; perf is over this span-covered subset — the two are never conflated. Performance figures are always all-audience.">perf coverage ${pctTxt} · ${spans.toLocaleString()} spans</span>`;
}

function _meRenderHeadline() {
  const el = document.getElementById('me-headline');
  if (!el || !_meData) return;
  const tc = _meData.total_cost || 0;
  const tt = _meData.total_turns || 0;
  const allRows = _meData.rows || [];
  const modelCount = allRows.length;
  const lowConfCount = allRows.filter(r => r.low_confidence).length;
  const confidentCount = modelCount - lowConfCount;
  // "Configured, unused" must count only models that CAN run (have credentials)
  // — an uncredentialed model is unusable, not idle, so it gets its own count
  // (uncredentialed-catalog honesty). Partition `unused` by the server's
  // `credentialed` flag; fail-open rows (flag absent/true) count as idle.
  const unusedRows = (_meData.unused || []);
  const unusedNoKey = unusedRows.filter(r => r.credentialed === false).length;
  const unusedIdle = unusedRows.length - unusedNoKey;
  // "Models in use" counts EVERY used model (10 = all used is correct). But the
  // leaderboard hides low-confidence rows by default, so the count and what's
  // shown can differ — annotate the split (N used · M confident · K low-volume)
  // so the headline reconciles with the "+N below the confidence line" affordance
  // instead of reading as an unexplained gap.
  const usedTip = lowConfCount > 0
    ? `${modelCount} used · ${confidentCount} with confident cost data (10k+ billed tokens) · ${lowConfCount} low-volume (shown below the confidence line on the leaderboard)`
    : `${modelCount} model${modelCount === 1 ? '' : 's'} used pod-wide, all with confident cost data`;
  el.innerHTML = `
    <div class="me-headline-row">
      <div class="me-stat"><div class="me-stat-label">Pod spend (${_meData.days}d)</div><div class="me-stat-value">${_usageFmt$(tc)}</div></div>
      <div class="me-stat"><div class="me-stat-label">Turns</div><div class="me-stat-value">${tt.toLocaleString()}</div></div>
      <div class="me-stat" title="${escHtml(usedTip)}"><div class="me-stat-label">Models in use</div><div class="me-stat-value">${modelCount}${lowConfCount > 0 ? `<span style="font-size:0.7rem;font-weight:400;color:var(--text3)"> · ${confidentCount} confident</span>` : ''}</div></div>
      <div class="me-stat" title="${escHtml(unusedIdle + ' configured model' + (unusedIdle === 1 ? '' : 's') + ' have credentials but no turns yet' + (unusedNoKey > 0 ? '; ' + unusedNoKey + ' more can\'t run — no API key on this pod for their provider' : ''))}"><div class="me-stat-label">Configured, unused</div><div class="me-stat-value">${unusedIdle}${unusedNoKey > 0 ? `<span style="font-size:0.7rem;font-weight:400;color:var(--text3)"> · ${unusedNoKey} no key</span>` : ''}</div></div>
    </div>`;
  // Pricing note now lives in the shared overview card's control cluster (the
  // headline element no longer carries it inline).
  const note = document.getElementById('me-pricing-note');
  if (note) {
    note.innerHTML = _meData.has_pricing
      ? '<span style="color:var(--text2)">list prices loaded</span>'
      : '<span style="color:var(--yellow)" title="The pricing cache is empty — list $/1k and eff-vs-list delta show as —. Refresh pricing on AI Optimization.">no list prices</span>';
  }
}

// ── Aggregate rollup strip (band ↔ role) — pod-wide overview ──────────────────

function _meSetRollupAxis(axis) {
  _meRollupAxis = axis;
  document.querySelectorAll('#me-rollup-toggle .me-seg-btn').forEach(b => {
    b.classList.toggle('is-active', b.dataset.axis === axis);
  });
  _meRenderRollup();
}

function _meRenderRollup() {
  const el = document.getElementById('me-rollup');
  if (!el || !_meData) return;
  const rollups = _meData.rollups || {};
  const groups = _meRollupAxis === 'role' ? (rollups.by_role || []) : (rollups.by_band || []);
  if (!groups.length) {
    el.innerHTML = '<div class="empty" style="padding:8px;font-size:0.8rem;color:var(--text3)">No rollup data.</div>';
    return;
  }
  el.innerHTML = groups.map(g => {
    const label = _meRollupAxis === 'role' ? (g.role || '—') : (g.band || 'unbanded');
    const members = (g.member_count || 0);
    const used = (g.used_count != null) ? g.used_count : members;
    const turns = (g.turns || 0).toLocaleString();
    // A band/role whose members are ALL low-volume blends to nothing (member_count
    // 0, $/turn null) — but it DOES have used models (used_count > 0). Render
    // "N models · insufficient data" with a dimmed cost rather than the misleading
    // "0 models · 0 turns" that erased the used-but-low-volume models (the empty
    // "High" card the operator reported). The blend stays confident-only — this is
    // a count/label change, not folding low-conf samples into the cost.
    // A role slot whose entire rung is uncredentialed can't be filled — every
    // member model lacks an API key on the pod. Read it as UNFILLED ("no
    // credentials"), not a populated slot that merely has no traffic. This
    // outranks the low-volume framing (no key ⇒ no usage is expected).
    const noCreds = !!g.uncredentialed;
    const allLowVol = !noCreds && members === 0 && used > 0;
    const cpt = (noCreds || allLowVol)
      ? `<span style="color:var(--text3)" title="${noCreds ? 'No API key on this pod for this slot\'s models — it can\'t run until you add one.' : 'No model in this group has enough volume (10k+ billed tokens) for an authoritative blended cost.'}">—</span>`
      : _meFmtPerTurn(g.cost_per_turn);
    const sub = noCreds
      ? `<span style="color:var(--text3)">no credentials</span>`
      : (allLowVol
        ? `${used} model${used === 1 ? '' : 's'} · <span style="color:var(--yellow)">insufficient data</span>`
        : `${members} model${members === 1 ? '' : 's'} · ${turns} turns`);
    const title = noCreds
      ? `${escHtml(label)}: this slot's rung models have no API key on the pod, so it can't run — add a credential to fill it.`
      : (allLowVol
        ? `${escHtml(label)}: ${used} used model${used === 1 ? '' : 's'}, all below the 10k-token confidence threshold — no authoritative blended cost (the per-model figures are on the leaderboard).`
        : `${escHtml(label)}: ${_usageFmt$(g.spend || 0)} over ${turns} turns across ${members} model${members === 1 ? '' : 's'}`);
    return `<div class="me-rollup-card" title="${title}">
      <div class="me-rollup-card-label">${escHtml(label)}</div>
      <div class="me-rollup-card-value">${cpt}<span style="font-size:0.7rem;font-weight:400;color:var(--text3)"> /turn</span></div>
      <div class="me-rollup-card-sub">${sub}</div>
    </div>`;
  }).join('');
}

// ── Filter chips (Reports/Alerts al-filter-chip pattern) ─────────────────────

function _meToggleChip(dim, val) {
  const s = _meFilters[dim];
  if (!s) return;
  if (s.has(val)) s.delete(val); else s.add(val);
  // Bot and audience are mutually exclusive (the matrix has no audience split,
  // the audience legs are pod-wide) — activating one clears the other.
  if (dim === 'bot' && s.size) _meFilters.audience.clear();
  if (dim === 'audience' && s.size) _meFilters.bot.clear();
  _meRenderFilters();
  _meRenderMetricSwitch();
  _meRenderActiveView();
}

function _meClearChips() {
  _meFilters.provider.clear();
  _meFilters.band.clear();
  _meFilters.bot.clear();
  _meFilters.audience.clear();
  _meRenderFilters();
  _meRenderMetricSwitch();
  _meRenderActiveView();
}

// Render one facet row. `entries` = [{val, label, count}]; `disabled` greys the
// whole facet (a facet the current selection can't combine with).
function _meChipRow(label, dim, entries, disabled) {
  if (!entries.length) return '';
  const sel = _meFilters[dim];
  const chips = entries.map(e => {
    const active = sel.has(e.val);
    const cnt = (e.count == null) ? '' : ` <span class="me-chip-count">(${e.count})</span>`;
    return `<button class="me-filter-chip${active ? ' is-active' : ''}" ${disabled ? 'disabled' : ''}
              onclick="_meToggleChip('${escHtml(dim)}','${escHtml(String(e.val)).replace(/'/g, "\\'")}')"
              title="${escHtml(disabled ? 'Disabled while the other of bot/audience is active' : 'Filter to ' + e.label)}">${escHtml(e.label)}${cnt}</button>`;
  }).join(' ');
  return `<div class="me-chip-row"><span class="me-chip-row-label">${escHtml(label)}</span>${chips}</div>`;
}

function _meRenderFilters() {
  const el = document.getElementById('me-filters');
  if (!el || !_meData) return;
  const rows = _meData.rows || [];

  // provider facet — distinct providers present, with row counts.
  const provCounts = new Map();
  for (const r of rows) { const p = _meRowProvider(r); if (p) provCounts.set(p, (provCounts.get(p) || 0) + 1); }
  const label = (typeof _aiProviderLabel === 'function') ? _aiProviderLabel : (x => x);
  const provEntries = [...provCounts.keys()].sort().map(p => ({ val: p, label: label(p), count: provCounts.get(p) }));

  // band facet — canonical bands present (cost order), plus unbanded if any.
  const bandCounts = new Map();
  for (const r of rows) { const b = r.band || ''; bandCounts.set(b, (bandCounts.get(b) || 0) + 1); }
  const bandEntries = [];
  for (const b of _ME_BANDS) { if (bandCounts.has(b)) bandEntries.push({ val: b, label: b, count: bandCounts.get(b) }); }
  if (bandCounts.has('')) bandEntries.push({ val: '', label: 'unbanded', count: bandCounts.get('') });

  // bot facet — distinct bots from the matrix.
  const botEntries = _meBotList.map(b => ({ val: b, label: b, count: null }));

  // audience facet — human / auto (auto == non_human).
  const audEntries = [{ val: 'human', label: 'human', count: null }, { val: 'auto', label: 'auto', count: null }];

  const audDisabled = _meBotActive();
  const botDisabled = _meAudienceSel() != null;

  const clearBtn = _meAnyFilter()
    ? `<button class="btn btn-ghost btn-sm" onclick="_meClearChips()" style="margin-top:2px">Clear filters</button>`
    : '';

  // bot/audience exclusivity hint.
  const xorHint = (audDisabled || botDisabled)
    ? `<div style="font-size:0.7rem;color:var(--text3);margin-top:2px">Bot and audience filters can't combine — the per-bot matrix has no audience split.</div>`
    : '';

  el.innerHTML = `<div class="card" style="padding:10px 12px">
    ${_meChipRow('provider', 'provider', provEntries, false)}
    ${_meChipRow('band', 'band', bandEntries, false)}
    ${_meChipRow('bot', 'bot', botEntries, botDisabled)}
    ${_meChipRow('audience', 'audience', audEntries, audDisabled)}
    ${xorHint}
    ${clearBtn}
  </div>`;
}

// ── View toggle (Bars | Table) + metric switcher ─────────────────────────────

function _meSetView(view) {
  _meView = view;
  document.querySelectorAll('#me-view-toggle .me-seg-btn').forEach(b => {
    b.classList.toggle('is-active', b.dataset.view === view);
  });
  _meRenderMetricSwitch();
  _meRenderActiveView();
}

function _meSetMetric(key) {
  _meMetric = key;
  _meRenderMetricSwitch();
  _meRenderBars();
}

// Reveal state for the low-confidence rows (default OFF). Driven by the "+N
// below the confidence line" affordance (_meLowConfAffordance) at the bottom of
// the bars/table. Re-renders the active view; the rollup excludes low-conf
// regardless, so it doesn't change.
function _meToggleLowConf(show) {
  _meShowLowConf = !!show;
  _meRenderActiveView();
}

// Render one metric-switcher segment for a group of metric defs.
function _meMetricSegBtns(metrics, active, effOff) {
  return metrics.map(m => {
    const disabled = m.eff && effOff;
    const isActive = m.key === active && !(m.eff && effOff);
    let title;
    if (disabled) {
      title = 'Effective cost/1k is unavailable per-audience and per-bot (no per-slice token volume). Clear the bot/audience filter to use it.';
    } else if (m.perf) {
      // Perf is over the span-covered subset and ALWAYS all-audience — say so on
      // the button so a long bar is never read as audience-scoped or as "good".
      title = `Sort bars by ${m.label} — ${m.note}. Over the span-covered subset (all-audience).`;
    } else {
      title = `Sort bars by ${m.label}`;
    }
    return `<button class="me-seg-btn${isActive ? ' is-active' : ''}" ${disabled ? 'disabled' : ''}
              onclick="_meSetMetric('${m.key}')" title="${escHtml(title)}">${escHtml(m.label)}</button>`;
  }).join('');
}

function _meRenderMetricSwitch() {
  const el = document.getElementById('me-metric-switch');
  if (!el) return;
  if (_meView !== 'bars') { el.style.display = 'none'; el.innerHTML = ''; return; }
  el.style.display = '';
  const effOff = _meEffUnavailable();
  const active = _meActiveMetric();
  const costMetrics = _ME_METRICS.filter(m => m.group !== 'perf');
  const perfMetrics = _ME_METRICS.filter(m => m.group === 'perf');
  const lbl = 'font-size:0.72rem;text-transform:uppercase;letter-spacing:0.05em;color:var(--text3);margin-right:2px';
  const hint = effOff
    ? `<span style="font-size:0.7rem;color:var(--text3);margin-left:8px">Eff. cost/1k unavailable while a ${_meBotActive() ? 'bot' : 'audience'} filter is active</span>`
    : '';
  // Perf group carries its own label so it never visually merges with the cost
  // metrics — the two are distinct units (all-turns cost vs span-subset perf).
  el.innerHTML = `<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
    <span style="${lbl}">Cost</span>
    <div class="me-seg">${_meMetricSegBtns(costMetrics, active, effOff)}</div>${hint}
    <span style="${lbl};margin-left:12px" title="Performance metrics from cascade turn-spans — over the span-covered subset (~partial coverage), always all-audience. Never conflated with the all-turns cost figures.">Perf <span style="text-transform:none;font-weight:400">(span subset)</span></span>
    <div class="me-seg">${_meMetricSegBtns(perfMetrics, active, effOff)}</div>
  </div>`;
}

function _meRenderActiveView() {
  const barsEl = document.getElementById('me-bars');
  const tableEl = document.getElementById('me-table');
  if (barsEl) barsEl.style.display = _meView === 'bars' ? '' : 'none';
  if (tableEl) tableEl.style.display = _meView === 'table' ? '' : 'none';
  const note = document.getElementById('me-view-note');
  if (note) {
    const n = _meViewRows().length;
    const filt = _meAnyFilter() ? ` (filtered: ${n} model${n === 1 ? '' : 's'})` : '';
    // Cost is over ALL turns; perf is over the span subset and ALWAYS all-audience.
    // Under a bot/audience filter the cost columns recast but perf does not — say
    // so visibly (the table always shows perf columns; the bars say it too).
    const perfAud = (_meBotActive() || _meAudienceSel() != null)
      ? '; performance is over the span subset and stays all-audience (only cost recasts to the filter)'
      : '; performance is over the span subset (≠ all-turns cost)';
    note.textContent = '$/turn is per-turn (sub-calls fold in); effective $/1k is incl. cache cost' + perfAud + filt;
  }
  if (_meView === 'bars') _meRenderBars(); else _meRenderTable();
}

// ── Bars view (primary) ──────────────────────────────────────────────────────

function _meRenderBars() {
  const el = document.getElementById('me-bars');
  if (!el) return;
  if (!_meData) { el.innerHTML = '<div class="empty" style="padding:14px">No data</div>'; return; }
  const metric = _meActiveMetric();
  const metricDef = _meMetricDef(metric);
  const isPerf = !!(metricDef && metricDef.perf);
  // Confident rows are the clean ranking; low-conf rows are surfaced below the
  // "+N below the confidence line" affordance (revealed dimmed) so they never
  // jump the $/turn sort.
  const all = _meFilteredRows();
  const confident = all.filter(r => !r.low_confidence);
  const lowconf = all.filter(r => r.low_confidence);
  if (!confident.length && !lowconf.length) {
    el.innerHTML = '<div class="empty" style="padding:14px">No models match the current filter.</div>';
    return;
  }
  // Sort each group desc by the metric; null metric values sort last. Perf
  // metrics read the per-row `perf` block via _meMetricValue (null when the
  // model has no spans).
  const byMetricDesc = (a, b) => {
    const av = _meMetricValue(a, metric), bv = _meMetricValue(b, metric);
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    return bv - av;
  };
  confident.sort(byMetricDesc);
  lowconf.sort(byMetricDesc);
  // Bar widths scale to the CONFIDENT max, so revealing the low-conf rows does
  // NOT reflow the confident ranking above. A low-conf bar that exceeds the
  // confident max is capped at the full track (dimmed + the "insufficient data"
  // badge; its numeric value is always shown), never overflowing. If there are
  // no confident rows (every used model is low-volume), scale to the low-conf set
  // instead so the bars aren't all zero-width.
  const scaleRows = confident.length ? confident : lowconf;
  const maxVal = scaleRows.reduce((m, r) => {
    const v = _meMetricValue(r, metric);
    return (v != null && v > m) ? v : m;
  }, 0);
  const _pctOf = (v) => (maxVal > 0 && v != null && v > 0) ? Math.min(100, (v / maxVal) * 100) : 0;

  // Stack the bar into a darker human segment + a lighter auto segment for the
  // Spend / Turns metrics — but ONLY when the bar value still reconciles with
  // the per-row audience legs (no bot or audience filter re-scoping the value;
  // those legs are pod-wide and would not match a re-scoped total). For the
  // ratio metrics ($/turn, eff-cost/1k, share) a human/auto split doesn't map
  // cleanly, so they stay single-segment.
  const splitMetric = (metric === 'total_cost' || metric === 'turns');
  const canSplit = splitMetric && !_meBotActive() && _meAudienceSel() == null;
  const audField = metric === 'total_cost' ? 'cost' : 'calls';

  const buildBar = (r) => {
    const v = _meMetricValue(r, metric);
    const pct = _pctOf(v);
    const provClass = _meProviderClass(r);
    const badges =
      (r.unexpected_billing ? ' <span class="badge badge-sm badge-warn" title="Drifted to API-key billing">key</span>' : '') +
      (r.configured === false ? ' <span class="badge badge-sm badge-neutral" title="Ran on the pod but is not in the configured catalog.">off-catalog</span>' : '');
    // Perf metric on a model with no spans is dimmed (no figure to show) the same
    // way a low-cost-confidence row is — the badge ("no span data") still shows.
    const lowConf = (r.low_confidence || (isPerf && (!r.perf || r.perf.low_coverage))) ? ' is-lowconf' : '';

    // Bar meta: for cost metrics show turns + the cost confidence badge; for perf
    // metrics show the span-coverage badge instead (the bar value IS a perf figure
    // over the span subset — never present it without its coverage context).
    const meta = isPerf
      ? `${_mePerfBadge(r)}${r.bot_count != null ? `<span>·</span><span>${r.bot_count} bot${r.bot_count === 1 ? '' : 's'}</span>` : ''}`
      : `<span>${(r.turns || 0).toLocaleString()} turns</span><span>·</span>${_meConfidenceBadge(r)}${r.bot_count != null ? `<span>·</span><span>${r.bot_count} bot${r.bot_count === 1 ? '' : 's'}</span>` : ''}`;

    let fill;
    if (canSplit && r.audience) {
      // Two stacked segments: human (darker, drawn left) + auto (lighter, drawn
      // right). Widths are each leg ÷ maxVal so they sum to the full bar width
      // (= v ÷ maxVal). Either leg can be 0 (renders nothing).
      const hv = (r.audience.human && r.audience.human[audField]) || 0;
      const av = (r.audience.non_human && r.audience.non_human[audField]) || 0;
      const hPct = _pctOf(hv);
      const aPct = _pctOf(av);
      const hTip = `human: ${escHtml(_meFmtMetric(metric, hv))}`;
      const aTip = `auto: ${escHtml(_meFmtMetric(metric, av))}`;
      fill = `<div class="me-bar-fill me-bar-fill-human ${provClass}" style="width:${hPct.toFixed(1)}%" title="${hTip}"></div>` +
             `<div class="me-bar-fill me-bar-fill-auto ${provClass}" style="left:${hPct.toFixed(1)}%;width:${aPct.toFixed(1)}%" title="${aTip}"></div>`;
    } else {
      fill = `<div class="me-bar-fill ${provClass}" style="width:${pct.toFixed(1)}%"></div>`;
    }

    return `<div class="me-bar-row${lowConf}">
      <div class="me-bar-label">${_meModelChip(r.model, r.provider)}${_meBandBadge(r.band)}${badges}</div>
      <div class="me-bar-main">
        <div class="me-bar-track" title="${escHtml(_meMetricLabel(metric))}: ${escHtml(_meFmtMetric(metric, v))}">
          ${fill}
          <div class="me-bar-value">${_meFmtMetric(metric, v)}</div>
        </div>
        <div class="me-bar-meta">${meta}</div>
      </div>
    </div>`;
  };

  // Confident bars (the clean ranking), then the always-visible "+N below the
  // confidence line" affordance, then — only when revealed — the dimmed low-conf
  // bars below it. The affordance names the hidden models so a used model is
  // never silently invisible.
  const confidentBars = confident.map(buildBar).join('');
  const lowconfBars = _meShowLowConf ? lowconf.map(buildBar).join('') : '';
  const affordance = _meLowConfAffordance(lowconf);

  // One-line legend when the stacked split is active (Spend/Turns), clarifying
  // the darker = human, lighter = auto convention.
  const legend = canSplit
    ? `<div class="me-bar-legend"><span class="me-bar-legend-swatch me-bar-legend-human"></span>human <span class="me-bar-legend-swatch me-bar-legend-auto"></span>auto <span style="color:var(--text3)">· bars split by who initiated the turn</span></div>`
    : '';
  // Perf caption — the honesty banner above a perf-metric bar set: the direction
  // ("lower = smoother" etc., so a long bar is never misread as good), the
  // span-subset framing (perf ≠ the all-turns cost figures), and — when a
  // bot/audience filter is active — that perf STAYS all-audience while cost recast
  // to the filter. Cost metrics show no caption.
  let caption = '';
  if (isPerf) {
    const filtered = _meBotActive() || _meAudienceSel() != null;
    const audNote = filtered
      ? ` · <span style="color:var(--yellow)">perf stays all-audience</span> (spans carry no audience split — only the cost figures recast to the ${_meBotActive() ? 'bot' : 'audience'} filter)`
      : '';
    caption = `<div class="me-perf-note"><strong>${escHtml(metricDef.label)}</strong> — ${escHtml(metricDef.note)} · over the span-covered subset, all-audience (not the all-turns cost figures)${audNote}</div>`;
  }
  el.innerHTML = `${caption}${legend}<div class="me-bars">${confidentBars}${affordance}${lowconfBars}</div>`;
}

// ── Table view (the v1 dense multi-stat table — kept as the fallback) ─────────

function _meSortBy(col) {
  if (_meSort === col) {
    _meSortDir = _meSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    _meSort = col;
    _meSortDir = 'desc';
  }
  _meRenderTable();
}

function _meToggleRow(key) {
  _meExpanded[key] = !_meExpanded[key];
  _meRenderTable();
}

// Sort a row group IN PLACE by the active table column (_meSort / _meSortDir).
// _meMetricValue reads perf columns off the per-row `perf` block and cost /
// identity columns off the row itself, so a perf-column sort works too. Applied
// to the confident and low-confidence groups independently so the confident
// ranking stays clean (a low-conf row never jumps the sort).
function _meTableSort(rows) {
  const dir = _meSortDir === 'desc' ? -1 : 1;
  rows.sort((a, b) => {
    const av = _meMetricValue(a, _meSort), bv = _meMetricValue(b, _meSort);
    // nulls always sort last regardless of direction.
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === 'string') return dir * av.localeCompare(bv);
    return dir * (av - bv);
  });
  return rows;
}

function _meSortCaret(col) {
  // Sort-direction indicator on the active header. Arrow glyphs (not the
  // expand-triangle family) — this is a column-sort cue, distinct from the
  // .expand-icon disclosure convention (style-guide §9.13).
  if (_meSort !== col) return '';
  return _meSortDir === 'desc' ? ' ↓' : ' ↑';
}

// One perf metric cell. Cost is over ALL turns; perf is over the span SUBSET, so
// the figure is dimmed when there's no span data or coverage is thin — never
// shown bare without the coverage context the adjacent "Perf cov." badge carries.
function _mePerfCell(r, key) {
  const p = r.perf;
  if (!p) return '<span style="color:var(--text3)" title="No spans for this model — performance unavailable">—</span>';
  const val = p[key];
  if (val == null) return '<span style="color:var(--text3)">—</span>';
  const txt = _meFmtMetric(key, val);
  if (p.low_coverage) {
    return `<span style="color:var(--text3)" title="Under 20 spans — shown but not authoritative">${txt}</span>`;
  }
  return txt;
}

function _meRenderTable() {
  const el = document.getElementById('me-table');
  if (!el) return;
  if (!_meData) { el.innerHTML = '<div class="empty" style="padding:14px">No data</div>'; return; }
  // Confident rows are the clean ranking; low-conf rows go below the "+N below
  // the confidence line" affordance (revealed dimmed) so they never jump the sort.
  const all = _meFilteredRows();
  const confident = _meTableSort(all.filter(r => !r.low_confidence));
  const lowconf = _meTableSort(all.filter(r => r.low_confidence));
  if (!confident.length && !lowconf.length) {
    el.innerHTML = '<div class="empty" style="padding:14px">No models match the current filter.</div>';
    return;
  }
  const effOff = _meEffUnavailable();
  // Perf is never audience-split — under a bot/audience filter the cost columns
  // recast but the perf columns stay all-audience. Label the perf group so the
  // operator can't read the perf numbers as scoped to the filter.
  const perfAllAud = _meBotActive() || _meAudienceSel() != null;

  const th = (label, col, opts) => {
    const o = opts || {};
    const sec = o.secondary ? ' data-secondary' : '';
    const tip = o.title ? ` title="${escHtml(o.title)}"` : '';
    if (!col) return `<th${sec}${tip}>${label}</th>`;
    return `<th${sec}${tip} style="cursor:pointer;user-select:none" onclick="_meSortBy('${col}')">${label}${_meSortCaret(col)}</th>`;
  };

  let html = '<div class="resp-table-wrap"><table class="resp-table resp-table-dense"><thead><tr>' +
    '<th style="width:14px"></th>' +
    th('Model', 'model') +
    th('Tier / band', 'band', { title: 'Cost band placement (low / medium / high / premium) from list price, observed cost, or family map.' }) +
    th('$/turn', 'cost_per_turn', { title: 'Cost PER TURN (sub-calls fold into the parent turn — NOT per-LLM-call). Headline metric, default sort.' }) +
    th('Eff. cost/1k', 'usd_per_1k_blended', { title: 'Effective blended COST per 1k tokens, INCL. cache cost (not a clean per-token list rate, and not an effectiveness/quality score). Expand a row for in/out split.' + (effOff ? ' Unavailable while an audience or bot filter is active (no per-slice token volume).' : '') }) +
    th('List $/1k', null, { title: 'Advertised list price (input midpoint). "—" when the pricing cache has no entry.', secondary: true }) +
    th('Eff−List', 'eff_vs_list_delta', { title: 'Effective minus list-midpoint on the blended $/1k axis. Negative = cache savings; positive = running over list.', secondary: true }) +
    th('Spend', 'total_cost') +
    th('Share', 'share_of_spend', { title: 'Share of total pod spend (recomputed over the filtered set).' }) +
    th('Turns', 'turns') +
    th('Bots', 'bot_count', { title: 'Distinct bots that ran this model.', secondary: true }) +
    th('Last used', 'last_used_ts', { secondary: true }) +
    th('Confidence', null, { title: 'Min-sample gate (10k billed tokens). Low-sample rows are flagged, never hidden.' }) +
    th('Human %', 'human_pct', { title: 'Share of this model\'s turns that were human-initiated.', secondary: true }) +
    // ── Performance columns (v2, span-covered SUBSET, ALWAYS all-audience) ──
    // Distinct from the all-turns cost columns; each row's perf coverage rides
    // in the "Perf cov." badge column. The group header carries an "all-audience"
    // marker when a bot/audience filter recasts the cost columns. These are
    // PRIMARY (not data-secondary): the ME table has no diagnostic-column toggle,
    // so a secondary perf column would be permanently hidden — the deliverable
    // needs them visible. The honest framing rides in the per-cell tooltips + the
    // "Perf cov." badge + the all-turns-vs-subset note in the card header.
    th(`Perf cov.${perfAllAud ? ' <span style="font-weight:400;color:var(--yellow)">(all-aud)</span>' : ''}`, null, { title: 'Span coverage for this model\'s performance figures. Cost is over ALL turns; perf is over the span-covered subset (~49% pod-wide). "no span data" = zero spans; "insufficient span data" = under 20 spans.' + (perfAllAud ? ' Perf stays ALL-AUDIENCE under a bot/audience filter — only the cost columns recast.' : '') }) +
    th('Struggle', 'struggle_avg', { title: 'Mean cascade struggle score (0–1) over the span subset. LOWER = smoother (a low score is good). All-audience.' }) +
    th('Lat p50', _ME_LAT_P50, { title: 'Median whole-turn wall-clock (incl. tool calls + waits — NOT pure inference) over the span subset. Lower = faster. All-audience.' }) +
    th('Lat p95', _ME_LAT_P95, { title: '95th-percentile whole-turn wall-clock (incl. tools/waits) over the span subset. Lower = faster. All-audience.' }) +
    th('Success', 'success_rate', { title: 'Share of spans (that carried a success field) marked successful. Higher = better. All-audience.' }) +
    th('Errors', 'error_rate', { title: 'Share of spans that recorded an error. Lower = fewer errors. All-audience.' }) +
    '</tr></thead><tbody>';

  // One table row (+ its expanded detail row when open). `dim` flags a
  // low-confidence row, which renders below the affordance, de-emphasized
  // (.me-lowconf-tr) — still fully marked by its "insufficient data" badge.
  const buildRow = (r, dim) => {
    const key = r.model;
    const open = !!_meExpanded[key];
    const expandIcon = `<span class="expand-icon${open ? ' is-open' : ''}" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>`;
    let rowHtml = `<tr${dim ? ' class="me-lowconf-tr"' : ''} style="cursor:pointer" onclick="_meToggleRow('${escHtml(key).replace(/'/g, "\\'")}')">
      <td data-label="" style="width:14px">${expandIcon}</td>
      <td data-label="Model">${_meModelChip(r.model, r.provider)}${r.unexpected_billing ? ' <span class="badge badge-sm badge-warn" title="Drifted to API-key billing">key</span>' : ''}${r.configured ? '' : ' <span class="badge badge-sm badge-neutral" title="Ran on the pod but is not in the configured catalog.">off-catalog</span>'}</td>
      <td data-label="Tier / band">${_meBandBadge(r.band)}</td>
      <td data-label="$/turn" style="font-weight:600">${_meFmtPerTurn(r.cost_per_turn)}</td>
      <td data-label="Eff. cost/1k">${r.usd_per_1k_blended == null ? '<span style="color:var(--text3)" title="Unavailable while an audience or bot filter is active">—</span>' : (r.low_confidence ? '<span style="color:var(--text3)" title="Low sample — not authoritative">' + _meFmtPer1k(r.usd_per_1k_blended) + '</span>' : _meFmtPer1k(r.usd_per_1k_blended))}</td>
      <td data-label="List $/1k" data-secondary style="color:var(--text2)">${_meFmtPer1k(r.list_per_1k_input)}</td>
      <td data-label="Eff−List" data-secondary>${_meFmtDelta(r.eff_vs_list_delta)}</td>
      <td data-label="Spend">${_usageFmt$(r.total_cost)}</td>
      <td data-label="Share" style="color:var(--text2)">${_meFmtPct(r.share_of_spend)}</td>
      <td data-label="Turns">${(r.turns || 0).toLocaleString()}</td>
      <td data-label="Bots" data-secondary style="color:var(--text2)">${r.bot_count == null ? '—' : r.bot_count}</td>
      <td data-label="Last used" data-secondary style="color:var(--text2)">${_meRecency(r.last_used_ts)}</td>
      <td data-label="Confidence">${_meConfidenceBadge(r)}</td>
      <td data-label="Human %" data-secondary style="color:var(--text2)">${r.human_pct == null ? '—' : r.human_pct.toFixed(0) + '%'}</td>
      <td data-label="Perf cov.">${_mePerfBadge(r)}</td>
      <td data-label="Struggle" style="color:var(--text2)">${_mePerfCell(r, 'struggle_avg')}</td>
      <td data-label="Lat p50" style="color:var(--text2)">${_mePerfCell(r, _ME_LAT_P50)}</td>
      <td data-label="Lat p95" style="color:var(--text2)">${_mePerfCell(r, _ME_LAT_P95)}</td>
      <td data-label="Success" style="color:var(--text2)">${_mePerfCell(r, 'success_rate')}</td>
      <td data-label="Errors" style="color:var(--text2)">${_mePerfCell(r, 'error_rate')}</td>
    </tr>`;
    if (open) {
      // Perf detail line (span subset, all-audience). success_n is the success_rate
      // DENOMINATOR — shown so a high rate over a tiny present-subset is legible.
      const p = r.perf;
      const perfDetail = p
        ? `<div style="margin-top:6px;display:flex;gap:24px;flex-wrap:wrap;border-top:1px solid var(--border);padding-top:6px">
          <div><span style="color:var(--text3)">Perf (span subset, all-audience)</span></div>
          <div><span style="color:var(--text3)">Spans</span> &nbsp;${(p.span_count || 0).toLocaleString()}</div>
          <div><span style="color:var(--text3)">Coverage</span> &nbsp;${p.coverage_pct == null ? '—' : (p.coverage_pct * 100).toFixed(0) + '% of turns'}</div>
          <div><span style="color:var(--text3)">Struggle</span> &nbsp;${_meFmtScore(p.struggle_avg)} <span style="color:var(--text3)">(lower = smoother)</span></div>
          <div><span style="color:var(--text3)">Latency p50 / p95</span> &nbsp;${_meFmtMs(p.latency_p50_ms)} / ${_meFmtMs(p.latency_p95_ms)} <span style="color:var(--text3)">(whole-turn)</span></div>
          <div><span style="color:var(--text3)">Success</span> &nbsp;${_meFmtRate(p.success_rate)}${p.success_n ? ` <span style="color:var(--text3)">(n=${p.success_n})</span>` : ' <span style="color:var(--text3)">(no field)</span>'}</div>
          <div><span style="color:var(--text3)">Errors</span> &nbsp;${_meFmtRate(p.error_rate)}</div>
        </div>`
        : `<div style="margin-top:6px;border-top:1px solid var(--border);padding-top:6px;color:var(--text3)">No cascade spans for this model — performance unavailable (cost above is still over ALL turns).</div>`;
      rowHtml += `<tr${dim ? ' class="me-lowconf-tr"' : ''}><td class="resp-table-fullspan" colspan="20" style="background:var(--bg2);font-size:0.78rem;color:var(--text2);padding:10px 14px">
        <div style="display:flex;gap:24px;flex-wrap:wrap">
          <div><span style="color:var(--text3)">Effective $/1k in</span> &nbsp;${_meFmtPer1k(r.usd_per_1k_input)} <span style="color:var(--text3)">(incl. cache)</span></div>
          <div><span style="color:var(--text3)">Effective $/1k out</span> &nbsp;${_meFmtPer1k(r.usd_per_1k_output)} <span style="color:var(--text3)">(incl. cache)</span></div>
          <div><span style="color:var(--text3)">List $/1k in</span> &nbsp;${_meFmtPer1k(r.list_per_1k_input)}</div>
          <div><span style="color:var(--text3)">List $/1k out</span> &nbsp;${_meFmtPer1k(r.list_per_1k_output)}</div>
          <div><span style="color:var(--text3)">Billed tokens</span> &nbsp;${r.billed_tokens == null ? '—' : r.billed_tokens.toLocaleString()}</div>
          <div><span style="color:var(--text3)">Full id</span> &nbsp;<code style="font-size:0.74rem">${escHtml(r.model)}</code></div>
        </div>
        ${perfDetail}
      </td></tr>`;
    }
    return rowHtml;
  };

  // Confident rows (the clean ranking), then the always-visible "+N below the
  // confidence line" affordance as a full-span row, then — only when revealed —
  // the dimmed low-conf rows below it.
  html += confident.map(r => buildRow(r, false)).join('');
  if (lowconf.length) {
    html += `<tr><td class="resp-table-fullspan" colspan="20" style="padding:6px 0 2px">${_meLowConfAffordance(lowconf)}</td></tr>`;
  }
  if (_meShowLowConf) html += lowconf.map(r => buildRow(r, true)).join('');
  html += '</tbody></table></div>';
  el.innerHTML = html;
}

// Status badge for a configured-but-unused model. Uncredentialed (the pod holds
// no API key for its provider) → muted "no credentials" with a provider-aware
// title; credentialed-but-idle → "no usage yet". Both are neutral badges (no new
// token/hex); the distinction is the label + title, not color.
function _meUnusedStatusBadge(r) {
  if (r.credentialed === false) {
    const need = r.provider
      ? `No ${r.provider} API key on this pod`
      : 'No API key for this model\'s provider on this pod';
    return `<span class="badge badge-sm badge-neutral" title="${escHtml(need)} — this model can't run until you add one.">no credentials</span>`;
  }
  return '<span class="badge badge-sm badge-neutral" title="Configured in the model catalog but no turns ran on it in this window.">no usage yet</span>';
}

function _meRenderUnused() {
  const el = document.getElementById('me-unused');
  if (!el || !_meData) return;
  const all = (_meData.unused || []);
  if (!all.length) { el.innerHTML = '<div class="empty" style="padding:10px;font-size:0.8rem;color:var(--text3)">Every configured model has usage.</div>'; return; }
  // Credentialed-but-idle first ("no usage yet"); uncredentialed models ("no
  // credentials" — can't run) sort to the bottom under a muted sub-heading. We
  // KEEP uncredentialed rows visible so the "add a key to unlock" affordance
  // survives — they are separated and dimmed, never dropped.
  const idle = all.filter(r => r.credentialed !== false);
  const noKey = all.filter(r => r.credentialed === false);
  const buildRow = (r, dim) => {
    const roles = (r.roles || []).map(x => `<span class="badge badge-sm badge-neutral">${escHtml(x)}</span>`).join(' ') || '<span style="color:var(--text3)">—</span>';
    return `<tr${dim ? ' class="me-lowconf-tr"' : ''}>
      <td data-label="Model">${_meModelChip(r.model, r.provider)}</td>
      <td data-label="Tier / band">${_meBandBadge(r.band)}</td>
      <td data-label="Roles">${roles}</td>
      <td data-label="List $/1k in" data-secondary style="color:var(--text2)">${_meFmtPer1k(r.list_per_1k_input)}</td>
      <td data-label="List $/1k out" data-secondary style="color:var(--text2)">${_meFmtPer1k(r.list_per_1k_output)}</td>
      <td data-label="Status">${_meUnusedStatusBadge(r)}</td>
    </tr>`;
  };
  let html = '<div class="resp-table-wrap"><table class="resp-table resp-table-dense"><thead><tr>' +
    '<th>Model</th><th>Tier / band</th><th>Roles</th><th data-secondary>List $/1k in</th><th data-secondary>List $/1k out</th><th>Status</th>' +
    '</tr></thead><tbody>';
  html += idle.map(r => buildRow(r, false)).join('');
  if (noKey.length) {
    html += `<tr><td class="resp-table-fullspan" colspan="6" style="padding:8px 4px 2px;color:var(--text3);font-size:0.78rem">Not available — no credentials <span style="color:var(--text3)">(no API key on this pod for these providers; add one to unlock)</span></td></tr>`;
    html += noKey.map(r => buildRow(r, true)).join('');
  }
  html += '</tbody></table></div>';
  el.innerHTML = html;
}

function _meSetDays(v, el) {
  _meDays = Number(v);
  document.querySelectorAll('#me-range-tabs .me-range-btn').forEach(b => b.classList.remove('active'));
  if (el) el.classList.add('active');
  loadModelEconomics();
}

// onclick handlers reachable from inline HTML need window exports (ESLint
// suppressions are baseline-shrink-only).
window.loadModelEconomics = loadModelEconomics;
window._meSetDays = _meSetDays;
window._meSetView = _meSetView;
window._meSetMetric = _meSetMetric;
window._meToggleLowConf = _meToggleLowConf;
window._meSetRollupAxis = _meSetRollupAxis;
window._meToggleChip = _meToggleChip;
window._meClearChips = _meClearChips;
window._meSortBy = _meSortBy;
window._meToggleRow = _meToggleRow;

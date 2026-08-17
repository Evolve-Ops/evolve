// ════════════════════════════════════════════════════════════════════════
// Page subtab: Sessions (page-monitoring)
//
// Renders the Cost → Sessions subtab. Three regions inside the subtab:
//   1. Per-bot vital-signs strip (cache hit rate, cost/day, session count)
//   2. Cache & cost economics panel (per-bot drill-down)
//   3. Session browser (paginated /api/turns view with filters)
//
// The `_mon*` prefix on locals + DOM ids (#mon-bot-tabs, #mon-range-tabs)
// predates the rename from "Monitoring" → "Sessions" and is preserved for
// stability with existing CSS / HTML.
//
// Hosted in pages/ alongside the other extracted page-tier modules. The
// SPA_SCRIPTS auto-discovery (Phase 4a) picks it up at server boot.
//
// State (top of file):
//   _monBot, _monDays   — bot tab + lookback selection
//   _monData            — cached strip + economics response (used by
//                         downstream session-browser drill-down)
//   _sbOffset, _sbTotal, _sbLimit — session browser pagination state
//
// Loaders dispatched via onSubTabActivate('cost', 'sessions'):
//   loadMonitoring()       — strip + economics; called on tab switch +
//                            range change
//   loadSessionBrowser()   — first-page session list
//   _sbFetch()             — page fetch (driven by sbPage(dir))
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - api(), toast(), escHtml(), botLabel() — core/
//   - _renderCostBotTiles() — pages/cost.js (range-change refreshes the
//                              parent Cost-page tiles too)
//   - _sbTimeAgo / _sbStat — orphan helpers still inline in the main
//                            script (~line 10923); used by other surfaces
//                            (Settings card etc.) so they stay shared
// ════════════════════════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════
// Sessions (page-monitoring) — renders the per-bot vital
// signs strip, the cache & cost economics panel, and the
// session browser. The `_mon*` prefix on locals predates
// the rename and is preserved for stability with existing
// DOM IDs (#mon-bot-tabs, #mon-range-tabs, etc).
// ══════════════════════════════════════════════════════
let _monBot = '';
let _monDays = 28;
let _monData = null; // cached for downstream consumers (session browser etc)

function monSetRange(days, btn) {
  _monDays = days;
  document.querySelectorAll('#mon-range-tabs .mon-range-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  if (typeof _renderCostBotTiles === 'function') _renderCostBotTiles();
  loadMonitoring();
}

function _monSelectBot(bot, btn) {
  _monBot = bot;
  document.querySelectorAll('#mon-bot-tabs .subtab').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  loadMonitoring();
}

function _monGoInfraJobs() {
  nav(document.querySelector('[data-page=maintenance]'));
  setTimeout(() => {
    subTab(document.querySelector('#page-maintenance [onclick*="infrajobs"]'), 'maintenance', 'infrajobs');
    loadInfraJobs();
  }, 80);
}

function _monBuildBotTabs(bots) {
  const bar = document.getElementById('mon-bot-tabs');
  if (!bar) return;
  const all = [''].concat(bots);
  bar.innerHTML = all.map(b => {
    const label = b ? b.charAt(0).toUpperCase() + b.slice(1) : 'All';
    const active = b === _monBot ? 'active' : '';
    return `<div class="subtab ${active}" onclick="_monSelectBot('${escHtml(b)}',this)">${escHtml(label)}</div>`;
  }).join('');
}

// Aggregate all rows (across bots) for a given array of DayRow[][]
function _monAggregate(rowArrays) {
  const byDate = {};
  rowArrays.forEach(rows => {
    if (!Array.isArray(rows)) return;
    rows.forEach(r => {
      if (!byDate[r.date]) byDate[r.date] = {
        date: r.date, sessions: 0, productive: 0, maintenance: 0, ambiguous: 0,
        first_response: 0, corrections: 0, efficiency_flags: 0, unexpected_billing: 0,
        cap_usage: {}, app_usage: {}, maint_signals: {}
      };
      const d = byDate[r.date];
      d.sessions    += r.session_count || 0;
      d.productive  += r.productive_sessions || 0;
      d.maintenance += r.maintenance_sessions || 0;
      d.ambiguous   += r.ambiguous_sessions || 0;
      d.first_response += r.first_response_resolutions || 0;
      d.corrections += r.correction_count || 0;
      d.efficiency_flags += r.efficiency_flag_count || 0;
      d.unexpected_billing += r.unexpected_billing_turns || 0;
      // Merge top_maintenance_signals (array of [signal, count] pairs)
      (r.top_maintenance_signals || []).forEach(([sig, cnt]) => {
        d.maint_signals[sig] = (d.maint_signals[sig] || 0) + cnt;
      });
      // Merge application_usage
      const cu = r.application_usage || {};
      Object.entries(cu).forEach(([cap, stats]) => {
        if (!d.cap_usage[cap]) d.cap_usage[cap] = { sessions: 0, correction_sessions: 0, efficiency_sessions: 0, unresolved_sessions: 0, productive_sessions: 0, maintenance_sessions: 0 };
        const c = d.cap_usage[cap];
        c.sessions             += stats.sessions || 0;
        c.correction_sessions  += stats.correction_sessions || 0;
        c.efficiency_sessions  += stats.efficiency_sessions || 0;
        c.unresolved_sessions  += stats.unresolved_sessions || 0;
      });
      // Merge app_usage
      const au = r.app_usage || {};
      Object.entries(au).forEach(([app, stats]) => {
        if (!d.app_usage[app]) d.app_usage[app] = { sessions: 0, correction_sessions: 0, efficiency_sessions: 0, unresolved_sessions: 0, productive_sessions: 0, maintenance_sessions: 0 };
        const a = d.app_usage[app];
        a.sessions             += stats.sessions || 0;
        a.correction_sessions  += stats.correction_sessions || 0;
        a.efficiency_sessions  += stats.efficiency_sessions || 0;
        a.unresolved_sessions  += stats.unresolved_sessions || 0;
        a.productive_sessions  += stats.productive_sessions || 0;
        a.maintenance_sessions += stats.maintenance_sessions || 0;
      });
    });
  });
  return Object.values(byDate).sort((a, b) => a.date.localeCompare(b.date));
}

// Sum totals from sorted aggregate rows
function _monTotals(sorted) {
  const t = { sessions: 0, productive: 0, maintenance: 0, ambiguous: 0, first_response: 0, corrections: 0, efficiency_flags: 0, unexpected_billing: 0, cap_usage: {}, app_usage: {}, maint_signals: {} };
  sorted.forEach(r => {
    t.sessions    += r.sessions;
    t.productive  += r.productive;
    t.maintenance += r.maintenance;
    t.ambiguous   += r.ambiguous;
    t.first_response += r.first_response;
    t.corrections += r.corrections;
    t.efficiency_flags += r.efficiency_flags || 0;
    t.unexpected_billing += r.unexpected_billing;
    Object.entries(r.maint_signals || {}).forEach(([sig, cnt]) => {
      t.maint_signals[sig] = (t.maint_signals[sig] || 0) + cnt;
    });
    // Merge application_usage
    Object.entries(r.cap_usage || {}).forEach(([cap, stats]) => {
      if (!t.cap_usage[cap]) t.cap_usage[cap] = { sessions: 0, correction_sessions: 0, efficiency_sessions: 0, unresolved_sessions: 0 };
      const c = t.cap_usage[cap];
      c.sessions            += stats.sessions || 0;
      c.correction_sessions += stats.correction_sessions || 0;
      c.efficiency_sessions += stats.efficiency_sessions || 0;
      c.unresolved_sessions += stats.unresolved_sessions || 0;
    });
    // Merge app_usage
    Object.entries(r.app_usage || {}).forEach(([app, stats]) => {
      if (!t.app_usage[app]) t.app_usage[app] = { sessions: 0, correction_sessions: 0, efficiency_sessions: 0, unresolved_sessions: 0, productive_sessions: 0, maintenance_sessions: 0 };
      const a = t.app_usage[app];
      a.sessions             += stats.sessions || 0;
      a.correction_sessions  += stats.correction_sessions || 0;
      a.efficiency_sessions  += stats.efficiency_sessions || 0;
      a.unresolved_sessions  += stats.unresolved_sessions || 0;
      a.productive_sessions  += stats.productive_sessions || 0;
      a.maintenance_sessions += stats.maintenance_sessions || 0;
    });
  });
  return t;
}

// Format a trend delta arrow with color class
function _monDelta(current, prev, higherIsBetter, fmt) {
  if (prev === 0 && current === 0) return '<span class="stat-delta neutral">—</span>';
  if (prev === 0) return '<span class="stat-delta neutral">new</span>';
  const diff = current - prev;
  if (Math.abs(diff) < 0.005 && fmt === 'pct') return '<span class="stat-delta neutral">—</span>';
  if (Math.abs(diff) < 1 && fmt === 'count') return '<span class="stat-delta neutral">—</span>';
  const up = diff > 0;
  const good = up ? higherIsBetter : !higherIsBetter;
  const cls = up ? (good ? 'up-good' : 'up-bad') : (good ? 'down-good' : 'down-bad');
  const arrow = up ? '▲' : '▼';
  const label = fmt === 'pct' ? `${arrow} ${Math.abs(diff*100).toFixed(0)}pp` : `${arrow} ${Math.abs(diff).toFixed(0)}`;
  return `<span class="stat-delta ${cls}">${label}</span>`;
}

// Color a percentage value
function _monColor(val, greenThresh, yellowThresh, higherIsBetter) {
  if (higherIsBetter) {
    if (val >= greenThresh) return 'var(--green)';
    if (val >= yellowThresh) return 'var(--yellow)';
    return 'var(--red)';
  } else {
    if (val <= greenThresh) return 'var(--green)';
    if (val <= yellowThresh) return 'var(--yellow)';
    return 'var(--red)';
  }
}

async function monRunMeasure(btn) {
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⏳ Running…';
  const r = await api('POST', '/api/analytics/measure/run', {});
  if (r?.ok) {
    btn.textContent = '⏳ Reloading…';
    await loadMonitoring();
    btn.textContent = '✓ Done';
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2000);
  } else {
    btn.textContent = '✗ Failed';
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 3000);
  }
}


async function loadMonitoring() {
  const bots = orderedBotIds(_statusData?.bots);
  _monBuildBotTabs(bots);
  const bot = _monBot;
  const days = _monDays;

  // Fetch both in parallel — metrics (session counts) and cache-economics
  // (cost + cache health). The vital-signs strip blends both; the four
  // panel cards come entirely from cache-economics.
  const [metrics, econ] = await Promise.all([
    api('GET', `/api/analytics/metrics?bot=${bot}&days=${days}`),
    api('GET', `/api/analytics/cache-economics?bot=${bot}&days=${days}`).catch(e => {
      console.warn('cache-economics fetch failed', e);
      return { summary: {}, total_cost_usd: 0 };
    }),
  ]);
  _monData = metrics;

  const botIds = bot ? [bot] : Object.keys(metrics || {});
  const rowArrays = botIds.map(id => metrics[id] || []);
  const sorted = _monAggregate(rowArrays);

  // Period-over-period: split window in half for the session-count delta.
  const mid = Math.floor(sorted.length / 2);
  const prevTotals = _monTotals(sorted.slice(0, mid));
  const curTotals  = _monTotals(sorted.slice(mid));
  const totals     = _monTotals(sorted);

  const statsEl = document.getElementById('mon-session-stats');
  if (!statsEl) return;

  // ── Vital signs strip ───────────────────────────────────────────────
  // 5 tiles, all observation-based:
  //   1. Total sessions (counted, with prior-half delta)
  //   2. Total cost (summed cost_usd over the window)
  //   3. Cache hit rate (realized; mirrors session_economics monitor)
  //   4. Invalidated cache % (TTL-too-short signal, also mirrored)
  //   5. Cost events (raw event count — the volume context for ratios)
  const sum = econ.summary || {};
  const totalCost = econ.total_cost_usd || 0;
  const hitRate = sum.realized_hit_rate || 0;
  const invRatio = sum.invalidated_ratio || 0;
  const totalEvents = sum.total_events || 0;
  const participating = sum.participating_events || 0;

  const sessDelta = curTotals.sessions - prevTotals.sessions;
  const sessDeltaHtml = sessDelta === 0 ? '' :
    `<span class="stat-delta neutral">${sessDelta > 0 ? '+' : ''}${sessDelta} vs prior half</span>`;
  // Hit rate: ≥50% green, ≥30% yellow, else red. Matches the chip in
  // the panel and session_economics cache_hit_rate_low threshold (0.50).
  const hitColor = !participating ? 'var(--text2)' : hitRate >= 0.50 ? 'var(--green)' : hitRate >= 0.30 ? 'var(--yellow)' : 'var(--red)';
  // Invalidated: ≥15% red, ≥8% yellow, else green. Matches
  // session_economics cache_invalidation_elevated threshold (0.15).
  const invColor = !participating ? 'var(--text2)' : invRatio >= 0.15 ? 'var(--red)' : invRatio >= 0.08 ? 'var(--yellow)' : 'var(--green)';

  if (!sorted.length && !totalEvents) {
    statsEl.innerHTML = `<div class="empty" style="grid-column:1/-1">No data yet. First metrics roll up after measure.py runs (daily at 01:00 AM); cache telemetry appears as cost events accrue.</div>`;
  } else {
    statsEl.innerHTML = `
      <div class="stat-block">
        <div class="stat-value">${totals.sessions.toLocaleString()}</div>
        <div class="stat-label">Total Sessions</div>
        ${sessDeltaHtml}
      </div>
      <div class="stat-block">
        <div class="stat-value">$${totalCost.toFixed(2)}</div>
        <div class="stat-label">Total Cost (${days}d)</div>
      </div>
      <div class="stat-block">
        <div class="stat-value" style="color:${hitColor}">${participating ? (hitRate*100).toFixed(0)+'%' : '—'}</div>
        <div class="stat-label">Cache Hit Rate${helpBtn('Realized cache_read / total prompt tokens over cache-participating events. Mirrors session_economics monitor — threshold 50%.')}</div>
      </div>
      <div class="stat-block">
        <div class="stat-value" style="color:${invColor}">${participating ? (invRatio*100).toFixed(0)+'%' : '—'}</div>
        <div class="stat-label">Invalidated Cache${helpBtn('Share of cached turns that paid cacheWrite cost without cacheRead savings. Above 15% suggests prompt cache TTL is too short.')}</div>
      </div>
      <div class="stat-block">
        <div class="stat-value">${totalEvents.toLocaleString()}</div>
        <div class="stat-label">Cost Events</div>
      </div>`;
  }

  // ── Cache & cost economics panel (Slice 3) ──────────────────────────
  // Render directly from the already-fetched response — no second fetch.
  _renderCacheEconomics(econ);

  // ── TTL recommendation (Slice 9) ─────────────────────────────────────
  // Single most actionable item on the page — fetched independently so
  // a slow openclaw.json read doesn't hold up the rest of the render.
  loadMonTtlRecommendation(bot, days).catch(e => console.warn('ttl rec load failed', e));

  // ── Anomaly strip (Slice 5) ──────────────────────────────────────────
  // Independent fetch — pulls firing session_economics signals filtered by
  // the active bot tab. Failure is non-fatal; the strip just stays empty.
  loadMonAnomalies(bot).catch(e => console.warn('anomaly strip load failed', e));

  // ── Activity rhythm (Slice 6) ────────────────────────────────────────
  // Independent fetch — heatmap, daily session counts, inter-turn gaps.
  // Failure is non-fatal; the panel renders empty if data is unavailable.
  loadMonRhythm(bot, days).catch(e => console.warn('rhythm load failed', e));

  // ── Sessions to read (Slice 7) ───────────────────────────────────────
  // Curated shortlist across four categories. Independent fetch.
  loadMonCuration(bot, days).catch(e => console.warn('curation load failed', e));

  // Populate bot filter and load browser
  const allBots = Object.keys(_statusData?.bots || {});
  _sbPopulateBotFilter(bot ? [bot] : allBots);
  loadSessionBrowser();

  // Empty state / warming up
  const rsiEl = document.getElementById('mon-rsi-status');
  if (rsiEl) {
    const rsiConfigured = Object.values(_statusData?.bots || {}).some(b => b.last_metric_date);
    const rsiActive = sorted.length >= 7;
    if (!rsiActive) {
      const statusLabel = rsiConfigured
        ? '<span style="color:var(--yellow)">collecting data</span>'
        : '<span style="color:var(--text2)">not configured</span>';
      const setupBtn = rsiConfigured ? '' :
        ' &nbsp;<button class="btn btn-ghost btn-sm" style="padding:2px 10px;font-size:0.78rem;vertical-align:middle" onclick="_monGoInfraJobs()">View Infra Jobs →</button>';
      rsiEl.innerHTML = `<div class="empty">Metrics data appears after 7+ days of collection. Status: ${statusLabel}${setupBtn}</div>`;
    } else {
      rsiEl.innerHTML = '';
    }
  }
}

// ── Session Browser ──────────────────────────────────
let _sbOffset = 0;
let _sbTotal  = 0;
const _sbLimit = 25;

async function loadSessionBrowser() {
  _sbOffset = 0;
  await _sbFetch();
}

async function sbPage(dir) {
  _sbOffset = Math.max(0, Math.min(_sbOffset + dir * _sbLimit, _sbTotal - 1));
  await _sbFetch();
}

async function _sbFetch() {
  const botVal       = document.getElementById('sb-bot')?.value || '';
  const multiTurn    = document.getElementById('sb-multi-turn')?.checked ? 'true' : '';
  const cacheInval   = document.getElementById('sb-cache-inval')?.checked ? 'true' : '';
  const params = new URLSearchParams({
    days: 7, limit: _sbLimit, offset: _sbOffset,
    ...(botVal     ? { bot: botVal }                       : {}),
    ...(multiTurn  ? { multi_turn: multiTurn }             : {}),
    ...(cacheInval ? { cache_invalidated: cacheInval }     : {}),
  });

  const wrap = document.getElementById('sb-table-wrap');
  const pager = document.getElementById('sb-pager');
  if (!wrap) return;
  wrap.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';
  if (pager) pager.style.display = 'none';

  const d = await api('GET', `/api/analytics/sessions?${params}`).catch(() => null);
  if (!d || d.error) {
    wrap.innerHTML = '<div class="empty">Failed to load sessions.</div>';
    return;
  }
  _sbTotal = d.total || 0;
  const sessions = d.sessions || [];

  if (!sessions.length) {
    wrap.innerHTML = '<div class="empty" style="padding:20px 0">No sessions match this filter.</div>';
    return;
  }

  const hasFallback = sessions.some(s => s._fallback);
  const fallbackNote = hasFallback
    ? `<div style="font-size:0.75rem;color:var(--yellow);margin-bottom:8px">⚠ Some sessions show partial data — session summaries are written when OC closes the session. Outcome will appear once sessions end.</div>`
    : '';

  const rows = sessions.map((s, i) => {
    const outcome = escHtml((s.outcome || '').slice(0, 70)) + ((s.outcome || '').length > 70 ? '…' : '');
    const date = (s.date || '').slice(5);  // MM-DD
    return `<tr onclick="_sbToggleRow(this,${i})" style="cursor:pointer">
      <td data-label="Date" style="color:var(--text2)">${date}</td>
      <td data-label="Bot"><span style="font-size:0.78rem">${escHtml(botLabel(s.bot_id))}</span></td>
      <td data-label="Cache">${_sbCacheChip(s.cache_state_counts)}</td>
      <td data-label="Turns" style="text-align:right">${s.turn_count || 1}</td>
      <td data-label="Cost" style="text-align:right">${_sbCostCell(s.total_cost_usd)}</td>
      <td data-label="Peak ctx" style="text-align:right;color:var(--text2);font-size:0.75rem">${_sbTokensCell(s.peak_prompt_tokens)}</td>
      <td data-label="Outcome" style="color:var(--text2)">${outcome}</td>
    </tr>
    <tr class="sb-row-expand" id="sb-expand-${i}" style="display:none">
      <td data-label="" colspan="7" class="sb-expand-body">
        ${s.outcome ? `<div style="margin-bottom:6px"><strong>Outcome:</strong> ${escHtml(s.outcome)}</div>` : ''}
        ${_sbCacheBreakdown(s.cache_state_counts)}
        ${(s.promises_made||[]).length ? `<div style="margin-bottom:6px"><strong>Promises:</strong> ${s.promises_made.map(p => escHtml(p)).join('; ')}</div>` : ''}
        <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:6px;font-size:0.75rem;color:var(--text3)">
          ${s.total_input_tokens  ? `<span>↓ ${s.total_input_tokens.toLocaleString()} in</span>` : ''}
          ${s.total_output_tokens ? `<span>↑ ${s.total_output_tokens.toLocaleString()} out</span>` : ''}
          ${s.first_response_resolution ? `<span style="color:var(--green)">1st-response resolution</span>` : ''}
          <span title="${escHtml(s.session_id)}" style="cursor:help">ID: ${escHtml((s.session_id || '').slice(0, 12))}…</span>
        </div>
      </td>
    </tr>`;
  }).join('');

  wrap.innerHTML = fallbackNote + `<div class="resp-table-wrap"><table class="resp-table">
    <thead><tr>
      <th>Date</th>
      <th>Bot</th>
      <th>Cache</th>
      <th style="text-align:right">Turns</th>
      <th style="text-align:right">Cost</th>
      <th style="text-align:right">Peak ctx</th>
      <th>Outcome</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;

  // Pager
  if (pager) {
    const from = _sbOffset + 1;
    const to   = Math.min(_sbOffset + _sbLimit, _sbTotal);
    document.getElementById('sb-page-label').textContent = `Showing ${from}–${to} of ${_sbTotal}`;
    document.getElementById('sb-prev').disabled = _sbOffset === 0;
    document.getElementById('sb-next').disabled = to >= _sbTotal;
    pager.style.display = 'flex';
  }
}

// ── Slice 8 cell renderers ──────────────────────────────────────────
// The cache chip summarizes the per-session cache_state breakdown into
// one glanceable token. Operator's eye lands on the color: red = waste
// (paid cacheWrite without cacheRead benefit), green = healthy, blue =
// fresh-only (no caching attempted, expected for 1-turn Q&A), grey =
// no cache_event records yet (very recent or older bot without sidecar).
function _sbCacheChip(counts) {
  counts = counts || { warm: 0, invalidated: 0, fresh: 0, unknown: 0 };
  const participating = (counts.warm || 0) + (counts.invalidated || 0);
  if (counts.invalidated > 0) {
    const pct = participating ? Math.round(counts.invalidated / participating * 100) : 0;
    return `<span title="${counts.invalidated}/${participating} invalidated" style="background:rgba(248,113,113,0.20);color:var(--red);padding:2px 7px;border-radius:8px;font-size:0.72rem;font-weight:500">${pct}% inval</span>`;
  }
  if (counts.warm > 0) {
    return `<span title="${counts.warm} warm cache hits" style="background:rgba(74,222,128,0.18);color:var(--green);padding:2px 7px;border-radius:8px;font-size:0.72rem;font-weight:500">warm</span>`;
  }
  if (counts.fresh > 0) {
    return `<span title="${counts.fresh} fresh; no caching attempted" style="background:rgba(126,184,247,0.18);color:var(--blue);padding:2px 7px;border-radius:8px;font-size:0.72rem;font-weight:500">fresh</span>`;
  }
  return '<span style="color:var(--text3);font-size:0.72rem">—</span>';
}

function _sbCostCell(c) {
  if (!c || c < 0.001) return '<span style="color:var(--text3)">$0</span>';
  if (c < 0.01) return `<span style="color:var(--text2)">$${c.toFixed(3)}</span>`;
  return `<span>$${c.toFixed(2)}</span>`;
}

function _sbTokensCell(t) {
  if (!t) return '—';
  if (t >= 1e6) return (t / 1e6).toFixed(1) + 'M';
  if (t >= 1e3) return (t / 1e3).toFixed(0) + 'k';
  return t.toString();
}

function _sbCacheBreakdown(counts) {
  if (!counts) return '';
  const parts = [];
  if (counts.warm)        parts.push(`<span style="color:var(--green)">${counts.warm} warm</span>`);
  if (counts.invalidated) parts.push(`<span style="color:var(--red)">${counts.invalidated} invalidated</span>`);
  if (counts.fresh)       parts.push(`<span style="color:var(--blue)">${counts.fresh} fresh</span>`);
  if (counts.unknown)     parts.push(`<span style="color:var(--text3)">${counts.unknown} unknown</span>`);
  if (!parts.length) return '';
  return `<div style="margin-bottom:6px;font-size:0.78rem"><strong>Cache:</strong> ${parts.join(' · ')}</div>`;
}

function _sbToggleRow(tr, idx) {
  const expand = document.getElementById(`sb-expand-${idx}`);
  if (!expand) return;
  // Empty string clears the inline display so the CSS rule wins.
  // resp-table sets display:block on phone (card-stack mode) and
  // table-row otherwise — letting CSS decide keeps both layouts working.
  const visible = expand.style.display !== 'none';
  expand.style.display = visible ? 'none' : '';
}

function _sbPopulateBotFilter(bots) {
  const sel = document.getElementById('sb-bot');
  if (!sel) return;
  // Prefer the bot's display name when set (operator typed the exact
  // casing they want); fall back to title-casing the bot id when there
  // is no display name, matching the prior behavior.
  sel.innerHTML = '<option value="">Any bot</option>' + bots.map(b => {
    const dn = botLabel(b);
    const label = dn !== b ? dn : (b.charAt(0).toUpperCase() + b.slice(1));
    return `<option value="${escHtml(b)}">${escHtml(label)}</option>`;
  }).join('');
}

// ════════════════════════════════════════════════════════════════════════
// Page: Cost (Usage & Cost surface)
//
// The Usage & Cost page renders bot-level cost / token / session tiles,
// per-bot billing detail, composition charts, model trigger charts,
// per-day drill-downs, the spend-alert footer card, and the compaction
// settings.
//
// State (6 lets at the top of the file):
//   _usageBot              — currently selected bot ("" = all)
//   _usageDays             — lookback window (default 28)
//   _usageBots             — populated after the first status load
//   _usageDayFilter        — filter for the drill-down table
//   _usageCompactionBot    — bot for the compaction-config inline form
//   _usageCompactionData   — cached per-bot compaction config
//
// Two color tables: _MODEL_COLORS (mirror of usage_analytics.py
// MODEL_COLORS_HEX) and the inline color helpers in _usageModelColor.
//
// Loaders dispatched via onPageActivate('cost') + the subtab activators
// in onSubTabActivate:
//   loadCost()           — high-level status load (drives the bot tabs)
//   loadUsageCost()      — heavy-detail load (charts, tables, drilldown)
//   _renderSpendAlert()  — bottom footer card
//
// Out of scope (still inline in the main script):
//   loadAnalyticsSessions / loadAnalyticsCost (line ~19560 / ~20220)
//     — subtab loaders that live in a separate analytics cluster
//   loadCostMeasures + the _cm* family (line ~37438+)
//     — the Cost Measures page (different surface, despite the name)
//   loadMonTtlRecommendation + _monTtl* family (line ~19806+)
//     — monitoring TTL recommendations, tied to alerts/maintenance
// ════════════════════════════════════════════════════════════════════════


// ══════════════════════════════════════════════════════
// Usage & Cost
// ══════════════════════════════════════════════════════

// State for the Usage & Cost page
let _usageBot = '';       // '' = all
let _usageDays = 28;
let _usageBots = [];      // populated after first status load

// Model color map (mirrors usage_analytics.py MODEL_COLORS_HEX)
const _MODEL_COLORS = {
  'anthropic/claude-haiku':  '#22c55e',
  'anthropic/claude-sonnet': '#06b6d4',
  'anthropic/claude-opus':   '#3b82f6',
  'anthropic_api_key':       '#ef4444',
  'openai':                  '#e5e7eb',
  'google':                  '#eab308',
  'xai':                     '#a855f7',
  'unknown':                 '#6b7280',
};

function _usageModelColor(key) {
  if (!key) return _MODEL_COLORS['unknown'];
  if (key.endsWith(':api_key')) return _MODEL_COLORS['anthropic_api_key'];
  if (key.includes('haiku'))   return _MODEL_COLORS['anthropic/claude-haiku'];
  if (key.includes('sonnet'))  return _MODEL_COLORS['anthropic/claude-sonnet'];
  if (key.includes('opus'))    return _MODEL_COLORS['anthropic/claude-opus'];
  if (key.startsWith('openai/')) return _MODEL_COLORS['openai'];
  if (key.startsWith('google/')) return _MODEL_COLORS['google'];
  if (key.startsWith('xai/'))   return _MODEL_COLORS['xai'];
  return _MODEL_COLORS['unknown'];
}

// Legacy alias — kept for callers in the Usage page; same 2-decimal-place
// rule as the global _fmtUsd. Prefer _fmtUsd for new code.
function _usageFmt$(n) { return n != null && n > 0 ? '$' + Number(n).toFixed(2) : '$0.00'; }

function usageSetRange(days, btn) {
  _usageDays = days;
  document.querySelectorAll('#usage-range-tabs .usage-range-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  document.getElementById('usage-custom-range').style.display = 'none';
  if (typeof _renderCostBotTiles === 'function') _renderCostBotTiles();
  loadUsageCost();
}

function usageToggleCustomRange(btn) {
  const el = document.getElementById('usage-custom-range');
  const visible = el.style.display === 'flex';
  el.style.display = visible ? 'none' : 'flex';
  document.querySelectorAll('#usage-range-tabs .usage-range-btn').forEach(b => b.classList.remove('active'));
  if (!visible && btn) btn.classList.add('active');
}

function usageApplyCustomRange() {
  const from = document.getElementById('usage-date-from')?.value;
  const to   = document.getElementById('usage-date-to')?.value;
  if (!from || !to) return;
  const days = Math.round((new Date(to) - new Date(from)) / 86400000) + 1;
  if (days < 1 || days > 365) { toast('Invalid date range (1–365 days).', 'err'); return; }
  _usageDays = days;
  document.getElementById('usage-custom-range').style.display = 'none';
  loadUsageCost();
}

function usageExportCSV() {
  const bot = _usageBot || 'all';
  window.location.href = `/api/analytics/usage/export?bot=${bot}&days=${_usageDays}`;
}

function _usageBuildBotTabs(bots) {
  const bar = document.getElementById('usage-bot-tabs');
  if (!bar) return;
  const all = [''].concat(bots);
  bar.innerHTML = all.map(b => {
    const label = b ? b.charAt(0).toUpperCase() + b.slice(1) : 'All';
    const active = b === _usageBot ? 'active' : '';
    return `<div class="subtab ${active}" onclick="_usageSelectBot('${escHtml(b)}',this)">${escHtml(label)}</div>`;
  }).join('');
}

function _usageSelectBot(bot, btn) {
  _usageBot = bot;
  document.querySelectorAll('#usage-bot-tabs .subtab').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  loadUsageCost();
}

// ── Cost page per-bot summary tiles ─────────────────────────────────────────
//
// Renders a compact grid of per-bot spend tiles above the range/bot tabs on
// the Cost page. Data comes from _statusData.bots[id].tile (already fetched
// for the Overview) so no new endpoint is needed.
//
// The tile stores 1d / 7d / 28d windows; we pick whichever best matches
// the currently-selected _usageDays range. Clicking a tile selects that bot
// in the existing subtab subnav and triggers loadUsageCost().
//
// Cost-spike chip rule (mirrors tile_metrics.py line 436-447):
//   cost_7d > $5 AND cost_7d > 2× cost_prior_7d → show "cost spike" chip
// We read it directly from tile.health_chips rather than re-computing it.

function _renderCostBotTiles() {
  const el = document.getElementById('cost-bot-tiles');
  if (!el) return;
  if (!_statusData) { el.innerHTML = ''; return; }

  const bots = _statusData.bots || {};
  // Same ordering as Overview: primary first, others alpha by label.
  const costBots = orderedBotIds(bots).map(id => [id, bots[id]]);
  if (!costBots.length) { el.innerHTML = ''; return; }

  // Are we rendering the Sessions subtab? Tile contents differ.
  const sessionsActive = document.getElementById('cost-sessions')?.classList.contains('active');

  // Pick 1d / 7d / 30d window based on the active subtab's range. Tiles
  // also track today (1d) so the per-bot summary reflects the user's
  // selection — otherwise "Today" would silently keep showing 7d data.
  // Custom ranges fall back to the closest fixed window.
  //
  // Windows are anchored on each bot's tile.anchor_date (the most recent
  // day with a metrics file) rather than on today's date, because the
  // measure cron writes each day's aggregate the following morning.
  // When that anchor lags today, we tell the operator via the tile
  // tooltip + delta label so the displayed window isn't opaque.
  const todayIso = new Date().toISOString().slice(0, 10);
  const anchorIso = (() => {
    for (const [, b] of costBots) {
      const a = b?.tile?.anchor_date;
      if (a) return a;
    }
    return todayIso;
  })();
  const anchorLagsToday = anchorIso < todayIso;
  const days = sessionsActive ? (_monDays || 28) : (_usageDays || 28);
  const win = days <= 1 ? '1d' : (days <= 7 ? '7d' : '28d');
  // The 1d window is now sourced from live turn JSONL (tile.cost.live_today
  // === true), matching the Usage Summary card. The anchor-lag-vs-today
  // suffix only applies to 7d/28d windows, which still use the rolled-up
  // aggregate that's a day behind. The legacy "prior day" relabel for
  // 1d-with-lagging-anchor is no longer reachable when live_today is set.
  const liveTodayAvailable = (() => {
    for (const [, b] of costBots) {
      if (b?.tile?.cost?.live_today === true) return true;
    }
    return false;
  })();
  const basePriorLabel = win === '1d' ? 'yesterday' : `prior ${win}`;
  const priorLabel = (win === '1d' && anchorLagsToday && !liveTodayAvailable)
    ? `prior day`
    : basePriorLabel;

  const trendHtml = (cur, prior) => {
    if (prior == null || prior === 0) {
      return cur > 0 ? `<span class="pod-trend-up">▲ new</span>` : '';
    }
    const pct = Math.round(((cur - prior) / prior) * 100);
    if (Math.abs(pct) < 5) return `<span class="pod-trend-flat">≈ flat</span>`;
    return pct > 0
      ? `<span class="pod-trend-up">▲ ${pct}% vs ${priorLabel}</span>`
      : `<span class="pod-trend-down">▼ ${Math.abs(pct)}% vs ${priorLabel}</span>`;
  };

  const selectedBot = sessionsActive ? (_monBot || '') : (_usageBot || '');

  // Pod totals for the "All" tile.
  let totSpendCur = 0, totSpendPrior = 0, totSessCur = 0, totSessPrior = 0;
  for (const [, b] of costBots) {
    const t = b.tile;
    const c = t ? t.cost : null;
    if (c) {
      totSpendCur   += (c[`usd_${win}`] || 0);
      totSpendPrior += (c[`usd_prior_${win}`] || 0);
    }
    const a = t ? t.activity : null;
    if (a) {
      totSessCur   += (a[`sessions_${win}`] || 0);
      totSessPrior += (a[`sessions_prior_${win}`] || 0);
    }
  }

  const renderTile = (id, b) => {
    const isAll = id === '';
    const tile = b ? b.tile : null;
    const c = tile ? tile.cost : null;
    const a = tile ? tile.activity : null;

    let curVal, priorVal, primary;
    if (sessionsActive) {
      curVal   = isAll ? totSessCur   : (a ? (a[`sessions_${win}`] || 0) : 0);
      priorVal = isAll ? totSessPrior : (a ? (a[`sessions_prior_${win}`] || 0) : null);
      primary  = curVal != null ? String(curVal) : '—';
    } else {
      curVal   = isAll ? totSpendCur   : (c ? (c[`usd_${win}`] || 0) : 0);
      priorVal = isAll ? totSpendPrior : (c ? (c[`usd_prior_${win}`] || 0) : null);
      primary  = curVal != null ? `$${curVal.toFixed(2)}` : '—';
    }

    const chipHtml = (!isAll && tile && Array.isArray(tile.health_chips)
      && tile.health_chips.some(ch => ch.id === 'cost_spike'))
      ? `<div><span class="cost-bot-tile-chip">cost spike</span></div>`
      : '';

    const isActive = id === selectedBot;
    const activeClass = isActive ? ' active' : '';
    const label = isAll ? 'All' : botLabel(id);
    const tileAnchor = tile?.anchor_date || anchorIso;
    // Spend tile is live when today + yesterday have been overlaid from
    // raw turn JSONL on top of the lagged aggregate. 1d uses
    // cost.live_today; 7d uses cost.live_today_7d; 28d uses
    // cost.live_today_28d. Without these the tile silently disagreed
    // with the Usage Summary card (2026-05-20 incident: tile $6.44 vs
    // summary $33.67 on 1d, and a separate $67 gap on 7d).
    const liveSpend = !sessionsActive && (
      (win === '1d'  && c?.live_today === true) ||
      (win === '7d'  && c?.live_today_7d === true) ||
      (win === '28d' && c?.live_today_28d === true)
    );
    const asOfSuffix = (tileAnchor && tileAnchor < todayIso && !liveSpend)
      ? ` — as of ${tileAnchor}`
      : '';
    const liveSuffix = liveSpend ? ' — live' : '';
    const titleAttr = sessionsActive
      ? `${label} — ${win} sessions${asOfSuffix}`
      : `${label} — ${win} spend${liveSuffix}${asOfSuffix}`;

    return `<div class="cost-bot-tile${activeClass}" data-bot="${escHtml(id)}" onclick="_usageTileSelect('${escHtml(id)}')" title="${escHtml(titleAttr)}">
      <div class="cost-bot-tile-name">${escHtml(label)}</div>
      <div class="cost-bot-tile-spend">${primary}</div>
      <div class="cost-bot-tile-delta">${trendHtml(curVal, priorVal)}</div>
      ${chipHtml}
    </div>`;
  };

  const tilesHtml = [renderTile('', null)].concat(
    costBots.map(([id, b]) => renderTile(id, b))
  ).join('');
  el.innerHTML = tilesHtml;
}

// Select a bot by clicking a cost tile. Drives BOTH the Spend and Sessions
// subtabs so the user has a single tile-based selector.
function _usageTileSelect(bot) {
  _usageBot = bot;
  _monBot = bot;
  document.querySelectorAll('#cost-bot-tiles .cost-bot-tile').forEach(t => {
    t.classList.toggle('active', (t.dataset.bot || '') === bot);
  });
  const sessionsActive = document.getElementById('cost-sessions')?.classList.contains('active');
  if (sessionsActive) loadMonitoring();
  else loadUsageCost();
}

async function loadCost() {
  // Load bots once so other consumers (export, custom range) can reference the list.
  if (!_usageBots.length) {
    try {
      const net = await api('GET', '/api/network');
      _usageBots = Object.keys(net.bots || {});
      if (!_usageBots.length) _usageBots = net.members || [];
    } catch(e) {}
  }
  _renderCostBotTiles();
  await loadUsageCost();
}

// ── Anthropic Admin cost report (Tier 2.2 Phase A) ──────────────────────────

function _anthAdminFormatUsd(n) {
  if (!Number.isFinite(n)) return '—';
  if (Math.abs(n) >= 100) return `$${n.toFixed(2)}`;
  return `$${n.toFixed(4)}`;
}

function _anthAdminEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

// Render the Anthropic admin card based on pod state. Called from
// loadUsageCost() so the card stays in sync as the operator navigates.
//
// Three states:
//   - hidden  — no bot in the pod is backed by Anthropic; the cross-check
//               would be comparing against a zero-byte local ledger
//   - setup   — Anthropic is in use, but the org-level admin key hasn't
//               been saved yet; render walkthrough + paste-key form
//   - ready   — admin key is on disk; render the fetch UI
async function _renderAnthropicAdminCard() {
  const card = document.getElementById('anthropic-admin-card');
  const body = document.getElementById('anthropic-admin-card-body');
  if (!card || !body) return;

  let status;
  try {
    status = await api('GET', '/api/anthropic-admin/status');
  } catch (_) {
    card.style.display = 'none';
    return;
  }
  if (!status || !status.pod_uses_anthropic) {
    card.style.display = 'none';
    return;
  }
  card.style.display = '';

  if (status.configured) {
    body.innerHTML = `
      <div class="card-title" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span>Anthropic Admin Cost Report</span>
        <span class="badge badge-muted" style="font-size:0.65rem">cross-check</span>
        <span style="flex:1"></span>
        <select id="anth-admin-days" class="input-w-sm" style="font-size:0.78rem">
          <option value="1">Today</option>
          <option value="7" selected>7d</option>
          <option value="30">30d</option>
          <option value="90">90d</option>
        </select>
        <button class="btn btn-ghost btn-sm" onclick="loadAnthropicAdminCost()" title="Fetch from console.anthropic.com">Fetch from Anthropic</button>
      </div>
      <div style="font-size:0.75rem;color:var(--text3);margin-bottom:10px">
        Cross-checks the locally-derived cost ledger against the
        authoritative numbers from Anthropic's Admin API.
      </div>
      <div id="anthropic-admin-cost-panel">
        <div style="font-size:0.82rem;color:var(--text2);padding:10px 4px">
          Click <strong>Fetch from Anthropic</strong> to load the cost report.
        </div>
      </div>`;
    return;
  }

  // Setup state — Anthropic is in use, but no admin key configured yet.
  body.innerHTML = `
    <div class="card-title" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span>Anthropic Admin Cost Report</span>
      <span class="badge badge-muted" style="font-size:0.65rem">setup</span>
    </div>
    <div style="font-size:0.82rem;color:var(--text2);margin-bottom:14px;line-height:1.5">
      Once set up, this card cross-checks the locally-derived cost ledger
      against Anthropic's authoritative org-level totals. It needs a
      separate <strong>admin key</strong> — distinct from the per-bot
      Anthropic API keys your bots use to call Claude.
    </div>
    <ol style="font-size:0.82rem;color:var(--text2);margin:0 0 14px 18px;line-height:1.6">
      <li>Open <a href="https://console.anthropic.com/settings/admin-keys" target="_blank" rel="noopener" style="color:#7fc8ff">console.anthropic.com → Settings → Admin Keys</a>.</li>
      <li>Click <strong>Create Key</strong> and name it something like "evolve cross-check".</li>
      <li>Copy the key — it starts with <code>sk-ant-admin-</code>.</li>
      <li>Paste it below. We'll save it to <code>${_anthAdminEsc(status.key_path || '/Users/Shared/evolve/anthropic-admin-key.json')}</code> (mode 600, evolve-owned).</li>
    </ol>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <input type="password" id="anth-admin-key-input" placeholder="sk-ant-admin-..." autocomplete="off" spellcheck="false"
             style="flex:1;min-width:280px;font-family:monospace;font-size:0.82rem;padding:6px 10px;background:var(--bg3);border:1px solid var(--border);border-radius:4px;color:var(--text1)" />
      <button class="btn btn-primary btn-sm" onclick="saveAnthropicAdminKey()">Save key</button>
    </div>
    <div id="anth-admin-key-msg" style="margin-top:10px;font-size:0.78rem;min-height:1.2em"></div>`;
}

async function saveAnthropicAdminKey() {
  const input = document.getElementById('anth-admin-key-input');
  const msg = document.getElementById('anth-admin-key-msg');
  if (!input || !msg) return;
  const apiKey = (input.value || '').trim();
  msg.style.color = 'var(--text3)';
  msg.textContent = '';
  if (!apiKey) {
    msg.style.color = 'var(--danger,#e57373)';
    msg.textContent = 'Paste an admin key first.';
    return;
  }
  msg.innerHTML = '<span class="spinner"></span> Saving…';
  const res = await api('POST', '/api/anthropic-admin/save-key', { api_key: apiKey });
  if (!res || !res.ok) {
    msg.style.color = 'var(--danger,#e57373)';
    msg.textContent = (res && res.error) || 'Save failed.';
    return;
  }
  input.value = '';
  msg.style.color = 'var(--success,#7fcf7f)';
  msg.textContent = 'Saved. Loading cost report…';
  await _renderAnthropicAdminCard();
  // Auto-fetch once we've switched to the ready state so the operator
  // sees the first report without an extra click.
  if (typeof loadAnthropicAdminCost === 'function') {
    await loadAnthropicAdminCost();
  }
}

async function loadAnthropicAdminCost() {
  const el = document.getElementById('anthropic-admin-cost-panel');
  if (!el) return;
  const daysEl = document.getElementById('anth-admin-days');
  const days = daysEl ? daysEl.value : '7';
  el.innerHTML = '<div class="loading"><span class="spinner"></span> Fetching from console.anthropic.com…</div>';
  let res;
  try {
    res = await api('GET', `/api/anthropic-admin/cost-report?days=${encodeURIComponent(days)}`);
  } catch (e) {
    const body = (e && e.body) || {};
    el.innerHTML = `
      <div class="error" style="padding:10px;font-size:0.82rem">
        <strong>Fetch failed:</strong> ${_anthAdminEsc(body.error || e.message || e)}
        ${body.status ? `<div style="font-size:0.72rem;color:var(--text3);margin-top:6px">HTTP ${body.status}</div>` : ''}
      </div>`;
    return;
  }
  if (!res || !res.ok) {
    el.innerHTML = `<div class="error" style="padding:10px;font-size:0.82rem">${_anthAdminEsc((res && res.error) || 'unknown error')}</div>`;
    return;
  }
  const r = res.report || {};
  const buckets = (r.raw && r.raw.data) || [];
  const total = r.total_cost_usd || 0;
  el.innerHTML = `
    <div style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:10px">
      <div>
        <div style="font-size:0.7rem;color:var(--text3);text-transform:uppercase">Range</div>
        <div style="font-family:monospace;font-size:0.78rem">${_anthAdminEsc(r.starting_at)} → ${_anthAdminEsc(r.ending_at)}</div>
      </div>
      <div>
        <div style="font-size:0.7rem;color:var(--text3);text-transform:uppercase">Bucket</div>
        <div style="font-family:monospace;font-size:0.78rem">${_anthAdminEsc(r.bucket_width)}</div>
      </div>
      <div>
        <div style="font-size:0.7rem;color:var(--text3);text-transform:uppercase">Buckets</div>
        <div style="font-family:monospace;font-size:0.78rem">${r.bucket_count}</div>
      </div>
      <div style="margin-left:auto">
        <div style="font-size:0.7rem;color:var(--text3);text-transform:uppercase">Anthropic-reported total</div>
        <div style="font-family:monospace;font-size:1rem;color:#7fc8ff">${_anthAdminFormatUsd(total)}</div>
      </div>
    </div>
    <details>
      <summary style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:0.78rem;color:var(--text2)"><span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>Raw API response (${buckets.length} bucket${buckets.length === 1 ? '' : 's'})</summary>
      <pre style="margin-top:8px;padding:10px;background:var(--bg3);border:1px solid var(--border);border-radius:4px;font-size:0.7rem;overflow:auto;max-height:400px">${_anthAdminEsc(JSON.stringify(r.raw, null, 2))}</pre>
    </details>
  `;
}


async function loadUsageCost() {
  const bot = _usageBot || 'all';
  const days = _usageDays;

  // Re-render the per-bot summary tiles at top whenever range or bot changes.
  // Uses already-loaded _statusData so this is instant (no extra fetch).
  _renderCostBotTiles();

  // Update billing card title
  const titleEl = document.getElementById('usage-billing-title');
  if (titleEl) titleEl.textContent = `Usage Summary — ${bot === 'all' ? 'All Bots' : bot} — last ${days} day${days !== 1 ? 's' : ''}`;

  // Show loading state
  const bodyEl = document.getElementById('usage-billing-body');
  if (bodyEl) bodyEl.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';

  const d = await api('GET', `/api/analytics/usage?bot=${bot}&days=${days}`);
  if (!d || d.error) {
    if (bodyEl) bodyEl.innerHTML = `<div class="empty">${d?.error || 'Failed to load usage data.'}</div>`;
    return;
  }

  // Cache so the unit toggle (Turns/Cost) can re-render charts without
  // re-fetching. Refreshed on every loadUsageCost() call.
  window._lastUsageData = d;

  // Snapshot for evo's cost pack (spec §3.4 reliability lever #3).
  // The model needs operator-visible totals + filters to answer
  // "what's our spend trend?", "is bot X expensive?", etc.
  try {
    window._evoContextSnapshots = window._evoContextSnapshots || {};
    const totals = d?.totals || d?.billing || {};
    window._evoContextSnapshots.cost = {
      bot_filter: bot,
      window_days: days,
      total_usd: totals.total_cost_usd ?? totals.total_cost ?? null,
      turns: totals.total_turns ?? null,
      sessions: totals.total_sessions ?? null,
      per_bot: (d?.per_bot || d?.bots || []).slice(0, 8).map(b => ({
        bot_id: b.bot_id || b.bot || '?',
        cost_usd: b.cost_usd ?? b.total_cost ?? null,
        turns: b.turns ?? b.turn_count ?? null,
      })),
    };
    if (typeof _evoDrawerUpdateContextChip === 'function') _evoDrawerUpdateContextChip();
  } catch (_) {}
  // Sync both composition toggles' active state with the persisted choices
  // on every render, in case the page navigated in cleanly. Metric (Turns |
  // Cost) drives the bar + both timelines; dimension (Trigger | Provider)
  // drives the composition breakdown.
  document.querySelectorAll('#usage-unit-toggle button').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.unit === (window._usageUnit || 'turns'));
  });
  document.querySelectorAll('#usage-comp-dim-toggle button').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.dim === (window._usageCompDim || 'provider'));
  });
  // Reset the day-drill panel — switching bot tabs / range tabs invalidates
  // any previously-selected day. User can click a bar to re-drill.
  if (typeof _usageDrillDay === 'function') _usageDrillDay(null);
  _renderUsageBilling(d);
  _renderUsageComposition(d);
  _renderUsageChart(d);
  _renderUsageTriggerChart(d);
  _renderUsageTables(d);
  await _renderUsageByApp();
  _renderContextHealth(d);
  await _renderSpendAlert();
  if (_usageBot) await _usageLoadCompaction(_usageBot);
  // Pod state may have changed (new bot deployed, key just saved). Fire
  // and forget — failures shouldn't block the rest of the page.
  _renderAnthropicAdminCard().catch(() => {});
}

// ── Activity composition source rollup ─────────────────────────────────────
//
// Two bucketings used on this page:
//
// * USAGE_BUCKETS_ORDERED (3 buckets: human / scheduled / background) —
//   mirrors tile_metrics._classify_split and powers the hierarchical
//   By Source table further down. The single "Scheduled" bucket groups
//   heartbeat + cron there because the table shows the underlying source
//   values as indented sub-rows under each bucket header.
//
// * USAGE_COMP_BUCKETS_ORDERED (4 buckets: human / heartbeat / cron /
//   background) — drives the Trigger view of the unified Usage Summary
//   composition card. Heartbeat and cron are kept distinct because their
//   cost profiles differ a lot in real pods (heartbeat: hourly autonomous
//   prompts, cron: operator-scheduled tasks) and showing them separately
//   is the main reason to look at this breakdown.
//
// Source values come from the `by_source` field on /api/analytics/usage;
// we roll them at render time so the API surface stays stable.

const USAGE_BUCKETS_ORDERED = ['human', 'scheduled', 'background'];
const USAGE_BUCKET_LABELS = {
  human:      'Human',
  scheduled:  'Scheduled',
  background: 'Background',
};
const USAGE_BUCKET_COLORS = {
  human:      'var(--accent)',
  scheduled:  'var(--cyan)',
  background: 'rgba(124, 92, 255, 0.32)',
};
const USAGE_SOURCE_BUCKETS = {
  user:      'human',
  human:     'human',
  heartbeat: 'scheduled',
  cron:      'scheduled',
  subagent:  'background',
  // Forge build/critique/refine dispatches — same channel as a user
  // turn (`unknown`) but tagged by the forge_sessions annotation pass
  // upstream so they don't inflate the Human bucket. Surface as a
  // distinct sub-row under Background in the By Source table.
  forge:     'background',
};
function usageBucketOf(source) {
  return USAGE_SOURCE_BUCKETS[source] || 'background';
}

// The Trigger view uses a 4-bucket split (heartbeat and cron kept
// distinct). Different colours per bucket so the four bar segments
// read cleanly even when the percent labels are small.
const USAGE_COMP_BUCKETS_ORDERED = ['human', 'heartbeat', 'cron', 'background'];
const USAGE_COMP_BUCKET_LABELS = {
  human:      'Human',
  heartbeat:  'Heartbeat',
  cron:       'Cron',
  background: 'Background',
};
const USAGE_COMP_BUCKET_COLORS = {
  human:      'var(--accent)',
  heartbeat:  'var(--cyan)',
  cron:       'var(--teal)',
  background: 'rgba(124, 92, 255, 0.32)',
};
const USAGE_COMP_SOURCE_BUCKETS = {
  user:      'human',
  human:     'human',
  heartbeat: 'heartbeat',
  cron:      'cron',
  subagent:  'background',
  forge:     'background',
};
function usageCompBucketOf(source) {
  return USAGE_COMP_SOURCE_BUCKETS[source] || 'background';
}

// Composition dimension toggle (Trigger | Provider) for the unified Usage
// Summary card. Persisted in localStorage, mirroring setUsageUnit's
// `evolveUsageUnit` pattern. Default 'provider' (the operator-approved
// default end state). The active METRIC (turns vs cost) is the separate
// window._usageUnit toggle — both axes drive _renderUsageComposition.
window._usageCompDim = (() => {
  try { return localStorage.getItem('evolveUsageCompDim') || 'provider'; }
  catch { return 'provider'; }
})();
function setUsageCompDim(dim) {
  if (dim !== 'trigger' && dim !== 'provider') return;
  window._usageCompDim = dim;
  try { localStorage.setItem('evolveUsageCompDim', dim); } catch { /* ignore */ }
  document.querySelectorAll('#usage-comp-dim-toggle button').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.dim === dim);
  });
  if (window._lastUsageData) _renderUsageComposition(window._lastUsageData);
}
window.setUsageCompDim = setUsageCompDim;

// Render the composition bar + legend for the active dimension
// (window._usageCompDim) weighted by the active metric (window._usageUnit).
// Both axes are read at render time, so either toggle re-runs this:
//   - dimension 'trigger'  → 4-bucket roll-up of d.by_source
//                'provider' → d.billing.by_provider rows ({calls, cost})
//   - metric    'turns' → segment.calls   'cost' → segment.cost
// Bar widths, the bold %, and legend sort all follow the active metric.
//
// Asymmetry: trigger buckets carry {calls, sessions, cost} but provider
// rows carry {calls, cost} and NO sessions — the legend cell omits the
// "· N sessions" segment when sessions is undefined.
function _renderUsageComposition(d) {
  const el = document.getElementById('usage-composition-body');
  if (!el) return;
  const dim  = window._usageCompDim || 'provider';
  const unit = window._usageUnit || 'turns';

  // Build the per-dimension segment list. Each segment: { label, calls,
  // cost, sessions?, color | providerClass }. Provider segments leave
  // sessions undefined and color via the shared .ai-provider-<slug> class
  // (sets --provider-color); trigger segments carry a token color string.
  let segments;
  if (dim === 'provider') {
    const byProvider = (d.billing && d.billing.by_provider) || {};
    segments = Object.entries(byProvider).map(([provider, p]) => ({
      label: provider,
      calls: p.calls || 0,
      cost:  p.cost  || 0,
      // sessions intentionally undefined — provider rows have no session count.
      providerClass: (typeof _aiProviderColorClass === 'function')
        ? _aiProviderColorClass(provider)
        : 'ai-provider-unknown',
    }));
  } else {
    const rows = d.by_source || [];
    const buckets = {};
    for (const k of USAGE_COMP_BUCKETS_ORDERED) {
      buckets[k] = { calls: 0, sessions: 0, cost: 0 };
    }
    for (const r of rows) {
      const b = usageCompBucketOf(r.source);
      buckets[b].calls    += r.calls    || 0;
      buckets[b].sessions += r.sessions || 0;
      buckets[b].cost     += r.cost     || 0;
    }
    segments = USAGE_COMP_BUCKETS_ORDERED.map(k => ({
      label:    USAGE_COMP_BUCKET_LABELS[k],
      calls:    buckets[k].calls,
      sessions: buckets[k].sessions,
      cost:     buckets[k].cost,
      color:    USAGE_COMP_BUCKET_COLORS[k],
    }));
  }

  // Metric share drives bar width, the bold %, and legend order.
  const metricOf = s => (unit === 'cost' ? s.cost : s.calls);
  const total = segments.reduce((a, s) => a + metricOf(s), 0);
  if (total <= 0) {
    el.innerHTML = '<div class="empty">No data</div>';
    return;
  }
  segments.forEach(s => { s.pct = metricOf(s) / total; });
  // Sort legend DESC by the active metric (the old provider table was
  // unsorted insertion order — this fixes that).
  segments.sort((a, b) => metricOf(b) - metricOf(a));

  // Per-segment color: provider rows reference --provider-color (set by the
  // .ai-provider-<slug> class); trigger rows carry an inline token color.
  const fillStyle = s => (s.providerClass ? 'background:var(--provider-color)' : `background:${s.color}`);
  const fillClass = s => (s.providerClass ? s.providerClass : '');

  // Stacked bar — skip zero-width segments to avoid 1px slivers from rounding.
  const barHtml = `<div class="usage-composition-bar">${
    segments.filter(s => s.pct > 0).map(s =>
      `<span class="${fillClass(s)}" style="width:${(s.pct * 100).toFixed(2)}%;${fillStyle(s)}" title="${escHtml(s.label)} ${(s.pct * 100).toFixed(0)}%"></span>`
    ).join('')
  }</div>`;
  // Grid of single-line cells. Each row ALWAYS shows turns and cost; sessions
  // only when present (trigger buckets have it, provider rows don't).
  const cellsHtml = segments.filter(s => s.pct > 0).map(s => {
    const sessPart = s.sessions != null ? ` · ${s.sessions.toLocaleString()} sessions` : '';
    const costPart = s.cost > 0 ? ` · ${_usageFmt$(s.cost)}` : '';
    return `<div class="usage-comp-cell">
      <span class="usage-comp-swatch ${fillClass(s)}" style="${fillStyle(s)}"></span>
      <strong class="usage-comp-label">${escHtml(s.label)}</strong>
      <span class="usage-comp-pct">${(s.pct * 100).toFixed(0)}%</span>
      <span class="usage-comp-num">${s.calls.toLocaleString()} turns${sessPart}${costPart}</span>
    </div>`;
  }).join('');
  el.innerHTML = barHtml + `<div class="usage-comp-grid">${cellsHtml}</div>`;
}

function _renderUsageBilling(d) {
  const b = d.billing || {};
  const bodyEl = document.getElementById('usage-billing-body');
  const alertEl = document.getElementById('usage-alert');
  if (!bodyEl) return;

  const total = d.total_turns || 0;
  const cost  = d.total_cost  || 0;
  const hasCost = b.has_cost_data;
  const unexpectedBilling = b.unexpected_billing_turns || 0;

  // Alert banners — a separate concern from the composition card; they
  // render into #usage-alert above the card and stay as-is.
  const alerts = [];
  if (total === 0) {
    alerts.push(`<div class="alert alert-warn">⏳ No turn data found for this period. Turns are written after each bot conversation.</div>`);
  }
  if (unexpectedBilling > 0) {
    alerts.push(`<div class="alert alert-warn">⚡ ${unexpectedBilling} turns flagged as unexpected billing mode by the plugin. Since MAX coverage ended every turn bills at API rates — this flag may be stale signal rather than a real anomaly.</div>`);
  }
  if (b.cron_cost > b.human_cost && b.cron_cost > 0) {
    alerts.push(`<div class="alert alert-warn" style="background:rgba(126,184,247,0.08);border-color:rgba(126,184,247,0.3);color:var(--blue)">ℹ️ Cron jobs account for majority of spend (${_usageFmt$(b.cron_cost)} cron vs ${_usageFmt$(b.human_cost)} human).</div>`);
  }
  if (alertEl) alertEl.innerHTML = alerts.join('');

  if (total === 0) {
    bodyEl.innerHTML = '<div class="empty">No turns in this period.</div>';
    return;
  }

  // One-line totals strip. The provider table + the two-up totals grid that
  // used to render here moved into the unified composition card below — the
  // breakdown is now the toggled bar+legend (Trigger | Provider × Turns | Cost).
  const totalLabel = hasCost ? _usageFmt$(cost) : '—';
  bodyEl.innerHTML = `
    <div style="font-size:0.82rem;color:var(--text2)">
      Total turns: <strong style="color:var(--text)">${total.toLocaleString()}</strong>
      · Total cost: <strong style="color:${cost>0?'var(--yellow)':'var(--text)'}">${totalLabel}</strong>
    </div>`;
}

function _renderUsageChart(d) {
  const byDate = d.by_date || [];
  // Read the global unit toggle and switch between count-based and
  // dollar-based stacking. Card title updates to match.
  const unit = window._usageUnit || 'turns';
  const titleEl = document.getElementById('usage-chart-title');
  if (titleEl) titleEl.textContent = unit === 'cost'
    ? 'Activity Timeline — Cost by Model'
    : 'Activity Timeline — Turns by Model';
  if (!byDate.length) {
    mkChart('chart-usage-timeline', 'bar', { labels: [], datasets: [] });
    document.getElementById('usage-chart-legend').innerHTML = '';
    return;
  }

  // Collect all model keys across all dates
  const modelKeys = [];
  const modelKeySet = new Set();
  byDate.forEach(row => {
    Object.keys(row.by_model || {}).forEach(k => {
      if (!modelKeySet.has(k)) { modelKeySet.add(k); modelKeys.push(k); }
    });
  });
  modelKeys.sort((a, b) => {
    // Sort: MAX first (no :api_key), then metered, then others
    const aApi = a.endsWith(':api_key');
    const bApi = b.endsWith(':api_key');
    if (aApi !== bApi) return aApi ? 1 : -1;
    return a.localeCompare(b);
  });

  // Pick the per-day dict based on the unit. by_model is turns; by_model_cost
  // is dollars. Both use the same model keys.
  const dataField = unit === 'cost' ? 'by_model_cost' : 'by_model';
  const labels = byDate.map(r => r.date.slice(5));
  const datasets = modelKeys.map(key => ({
    label: key.endsWith(':api_key')
      ? key.replace(':api_key', '') + ' [API key]'
      : key.split('/').pop(),
    data: byDate.map(r => (r[dataField] || {})[key] || 0),
    backgroundColor: _usageModelColor(key) + 'aa',
    borderColor:     _usageModelColor(key),
    borderWidth: 1,
    stack: 's',
  }));

  mkChart('chart-usage-timeline', 'bar', { labels, datasets }, {
    scales: {
      x: { stacked: true, ticks: { color: '#666', font: { size: 10 } }, grid: { color: '#222' } },
      y: {
        stacked: true,
        ticks: {
          color: '#666',
          font: { size: 10 },
          callback: v => unit === 'cost' ? '$' + Number(v).toFixed(2) : v,
        },
        grid: { color: '#222' },
      },
    },
    plugins: {
      tooltip: {
        callbacks: {
          title: ctx => byDate[ctx[0].dataIndex]?.date || '',
          afterBody: ctx => {
            const row = byDate[ctx[0].dataIndex];
            if (!row) return '';
            const totalLine = unit === 'cost'
              ? 'Total: $' + Number(row.total_cost || 0).toFixed(2)
              : 'Total: ' + row.total;
            return ['', totalLine].concat(
              Object.entries(row[dataField] || {})
                .sort((a,b) => b[1]-a[1])
                .map(([k,v]) => unit === 'cost'
                  ? `  ${k.split('/').pop()}: $${Number(v).toFixed(2)}`
                  : `  ${k.split('/').pop()}: ${v}`)
            );
          },
        },
      },
      legend: { display: false },
    },
    onClick: (evt, els) => {
      if (els.length) _usageDrillDay(byDate[els[0].index]);
    },
  });

  // Legend
  const legendEl = document.getElementById('usage-chart-legend');
  if (legendEl) {
    legendEl.innerHTML = modelKeys.map(k => {
      const color = _usageModelColor(k);
      const label = k.endsWith(':api_key')
        ? k.replace(':api_key', '').split('/').pop() + ' [API key]'
        : k.split('/').pop();
      return `<span style="display:flex;align-items:center;gap:4px">
        <span style="width:10px;height:10px;border-radius:2px;background:${color};flex-shrink:0"></span>
        <span style="color:var(--text2)">${label}</span>
      </span>`;
    }).join('');
  }
}

// Activity Timeline stacked by trigger bucket (human / scheduled / background)
// — same colour vocabulary as the composition card and dashboard tile, so
// the eye reads them as the same dimension across surfaces. Uses the
// `by_trigger` field added to each by_date entry by compute_summary.
function _renderUsageTriggerChart(d) {
  const byDate = d.by_date || [];
  const unit = window._usageUnit || 'turns';
  const titleEl = document.getElementById('usage-trigger-chart-title');
  if (titleEl) titleEl.textContent = unit === 'cost'
    ? 'Activity Timeline — Cost by Trigger'
    : 'Activity Timeline — Turns by Trigger';
  if (!byDate.length) {
    mkChart('chart-usage-trigger-timeline', 'bar', { labels: [], datasets: [] });
    const legendEl = document.getElementById('usage-trigger-legend');
    if (legendEl) legendEl.innerHTML = '';
    return;
  }
  // Brand colours, hard-coded for Chart.js (which doesn't resolve var(--…)).
  // Mirror the .pod-bar-* CSS values in this file.
  const buckets = [
    { key: 'human',      label: 'Human',      color: '#7C5CFF' },
    { key: 'scheduled',  label: 'Scheduled',  color: '#4CC9F0' },
    { key: 'background', label: 'Background', color: '#7C5CFF52' }, // 32% alpha
  ];
  const dataField = unit === 'cost' ? 'by_trigger_cost' : 'by_trigger';
  const labels = byDate.map(r => r.date.slice(5));
  const datasets = buckets.map(b => ({
    label: b.label,
    data: byDate.map(r => (r[dataField] || {})[b.key] || 0),
    backgroundColor: b.color,
    borderColor:     b.color,
    borderWidth: 1,
    stack: 's',
  }));

  mkChart('chart-usage-trigger-timeline', 'bar', { labels, datasets }, {
    scales: {
      x: { stacked: true, ticks: { color: '#666', font: { size: 10 } }, grid: { color: '#222' } },
      y: {
        stacked: true,
        ticks: {
          color: '#666',
          font: { size: 10 },
          callback: v => unit === 'cost' ? '$' + Number(v).toFixed(2) : v,
        },
        grid: { color: '#222' },
      },
    },
    plugins: {
      tooltip: {
        callbacks: {
          title: ctx => byDate[ctx[0].dataIndex]?.date || '',
          afterBody: ctx => {
            const row = byDate[ctx[0].dataIndex];
            if (!row) return '';
            const t = row[dataField] || {};
            const total = (t.human || 0) + (t.scheduled || 0) + (t.background || 0);
            const totalLine = unit === 'cost'
              ? 'Total: $' + Number(total).toFixed(2)
              : 'Total: ' + total;
            return ['', totalLine]
              .concat(buckets.map(b => unit === 'cost'
                ? `  ${b.label}: $${Number(t[b.key] || 0).toFixed(2)}`
                : `  ${b.label}: ${t[b.key] || 0}`));
          },
        },
      },
      legend: { display: false },
    },
    onClick: (evt, els) => {
      if (els.length) _usageDrillDay(byDate[els[0].index]);
    },
  });

  const legendEl = document.getElementById('usage-trigger-legend');
  if (legendEl) {
    legendEl.innerHTML = buckets.map(b =>
      `<span style="display:flex;align-items:center;gap:4px">
        <span style="width:10px;height:10px;border-radius:2px;background:${b.color};flex-shrink:0"></span>
        <span style="color:var(--text2)">${b.label}</span>
      </span>`
    ).join('');
  }
}

let _usageDayFilter = null;

// Click a day on either timeline → render the top sessions for that day
// in the Day Detail card below. Click the same day again (or the Clear
// button) to dismiss.
function _usageDrillDay(row) {
  const titleEl = document.getElementById('usage-day-drill-title');
  const bodyEl = document.getElementById('usage-day-drill-body');
  const clearBtn = document.getElementById('usage-day-drill-clear');
  // Toggle off — either explicit clear (row=null) or repeat-click on
  // the same day.
  if (!row || (_usageDayFilter && _usageDayFilter === row.date)) {
    _usageDayFilter = null;
    if (titleEl) titleEl.textContent = 'Day detail';
    if (clearBtn) clearBtn.style.display = 'none';
    if (bodyEl) {
      bodyEl.innerHTML = `<div class="empty" style="padding:8px 0;font-size:0.82rem;color:var(--text3)">
        Click a day on either timeline above to see the top sessions for that day.
      </div>`;
    }
    return;
  }
  _usageDayFilter = row.date;
  if (titleEl) titleEl.textContent = `Day detail — ${row.date}`;
  if (clearBtn) clearBtn.style.display = '';
  if (!bodyEl) return;

  const sessions = row.top_sessions || [];
  if (!sessions.length) {
    bodyEl.innerHTML = `<div class="empty" style="padding:8px 0;font-size:0.82rem">
      No session data captured for this day.
    </div>`;
    return;
  }
  // Day summary line
  const t = row.by_trigger || {};
  const tc = row.by_trigger_cost || {};
  const summaryParts = ['Human', 'Scheduled', 'Background'].map((label, i) => {
    const k = ['human', 'scheduled', 'background'][i];
    const turns = t[k] || 0;
    const cost = tc[k] || 0;
    return `<span style="font-size:0.78rem">
      <span class="usage-comp-swatch" style="background:${USAGE_BUCKET_COLORS[k]}"></span>
      ${label} ${turns} turn${turns === 1 ? '' : 's'} · ${_fmtUsd(cost)}
    </span>`;
  });
  const summaryHtml = `<div style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:10px;color:var(--text2)">
    <span style="font-size:0.78rem"><strong>${row.total}</strong> turns total · <strong>${_fmtUsd(row.total_cost)}</strong></span>
    ${summaryParts.join('')}
  </div>`;
  // Top sessions table
  const rows = sessions.map(s => `<tr>
    <td data-label=""><span class="usage-comp-swatch" style="background:${USAGE_BUCKET_COLORS[s.bucket] || 'var(--text3)'}"></span></td>
    <td data-label="Session" style="font-family:monospace;font-size:0.75rem;color:var(--text2)" title="${escHtml(s.session_id)}">${escHtml(String(s.session_id || '').slice(0, 12))}…</td>
    <td data-label="Bot" style="font-size:0.78rem">${escHtml(s.instance || '—')}</td>
    <td data-label="Source" style="font-size:0.78rem;color:var(--text2)">${escHtml(s.source || '—')}</td>
    <td data-label="Turns" style="font-size:0.78rem">${(s.turns || 0).toLocaleString()}</td>
    <td data-label="Cost" style="font-size:0.78rem">${_fmtUsd(s.cost || 0)}</td>
  </tr>`).join('');
  bodyEl.innerHTML = `${summaryHtml}
    <div style="font-size:0.75rem;color:var(--text3);margin-bottom:6px">Top ${sessions.length} session${sessions.length === 1 ? '' : 's'} by cost:</div>
    <div class="resp-table-wrap"><table class="resp-table">
      <thead><tr><th></th><th>Session</th><th>Bot</th><th>Source</th><th>Turns</th><th>Cost</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
}

function _fmtTok(n) {
  if (!n) return '—';
  if (n >= 1_000_000) return (n/1_000_000).toFixed(1) + 'M';
  if (n >= 1_000)     return (n/1_000).toFixed(1) + 'k';
  return String(n);
}

// Format an observed $/1k-token figure for a table cell. `rate` is the
// usd_per_1k_* value from usage_analytics (or null when there's no cost /
// no tokens). `lowConfidence` is the per-model min-sample flag — when set
// we don't render a number at all (it'd be noise off a handful of tokens),
// instead showing a dimmed "—" with an "insufficient data" hover so the
// operator knows the figure exists but isn't trustworthy yet.
function _usagePer1kCell(rate, lowConfidence) {
  if (lowConfidence) {
    return `<span style="color:var(--text3)" title="Insufficient token volume for a reliable per-1k figure">—</span>`;
  }
  if (rate == null || rate <= 0) return '<span style="color:var(--text3)">—</span>';
  // Sub-cent rates need more precision than the $0.00 _usageFmt$ format.
  const txt = rate < 0.01 ? '$' + Number(rate).toFixed(4) : '$' + Number(rate).toFixed(3);
  return `<span style="color:var(--text2)">${txt}</span>`;
}

// ── "Show more columns" toggle (Cost-page polish over §4.3.a) ──────────────
// Tables that mark columns with data-secondary can be paired with a small
// toggle that flips data-show-secondary on the <table.resp-table>. State
// persists per panel in localStorage so an operator who likes the full
// detail view keeps it across sessions.
//
// Each toggle button declares its panel via data-resp-cols-panel="<name>".
// The button is expected to live inside a .card; on click we find every
// .resp-table inside that card and apply the state. Distinct panel names
// per card mean two panels' localStorage writes never collide.
const RESP_COLS_LS_PREFIX = 'evolve.respCols.';
function _respColsRead(panel) {
  try { return localStorage.getItem(RESP_COLS_LS_PREFIX + panel) === 'true'; }
  catch (e) { return false; }
}
function _respColsWrite(panel, on) {
  try { localStorage.setItem(RESP_COLS_LS_PREFIX + panel, on ? 'true' : 'false'); }
  catch (e) { /* ignore quota/privacy errors */ }
}
function _respColsApplyPanel(panel) {
  const btn = document.querySelector(`[data-resp-cols-panel="${panel}"]`);
  if (!btn) return;
  const panelRoot = btn.closest('.card');
  if (!panelRoot) return;
  const on = _respColsRead(panel);
  for (const t of panelRoot.querySelectorAll('table.resp-table')) {
    if (on) t.setAttribute('data-show-secondary', 'true');
    else t.removeAttribute('data-show-secondary');
  }
  btn.textContent = on ? 'Hide extra columns' : 'Show more columns';
  btn.setAttribute('aria-pressed', on ? 'true' : 'false');
}
function _respColsApplyAll() {
  for (const btn of document.querySelectorAll('[data-resp-cols-panel]')) {
    _respColsApplyPanel(btn.getAttribute('data-resp-cols-panel'));
  }
}
function toggleRespCols(btn) {
  const panel = btn.getAttribute('data-resp-cols-panel');
  if (!panel) return;
  _respColsWrite(panel, !_respColsRead(panel));
  _respColsApplyPanel(panel);
}

// A Slack thread_ts is "<epoch_seconds>.<micros>". Show it as a short local
// datetime so the operator can tell threads apart; fall back to a truncated
// raw value if it isn't an epoch.
function _usageThreadLabel(ts) {
  const sec = parseFloat(ts);
  if (isFinite(sec) && sec > 1e9) {
    return new Date(sec * 1000).toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  }
  return String(ts || '').slice(0, 12);
}

// Expand/collapse a By Channel parent row's rolled-up thread sub-rows. DOM
// toggle (no re-render): flips the sub-rows' hidden attr + the .expand-icon's
// is-open class. Window-exported for the inline onclick.
function toggleUsageChanThreads(idx) {
  const tbody = document.querySelector('#usage-table-channel tbody');
  if (!tbody) return;
  const rowEl = tbody.querySelector(`tr.usage-chan-row[data-chan="${idx}"]`);
  const icon = rowEl ? rowEl.querySelector('.expand-icon') : null;
  const willOpen = icon ? !icon.classList.contains('is-open') : true;
  tbody.querySelectorAll(`tr.usage-chan-thread[data-chan="${idx}"]`)
    .forEach(tr => { tr.hidden = !willOpen; });
  if (icon) icon.classList.toggle('is-open', willOpen);
}
window.toggleUsageChanThreads = toggleUsageChanThreads;

function _renderUsageTables(d) {
  // By Model table — Calls · Cost · observed $/1k (in/out) · token volume.
  // The observed $/1k columns are computed by usage_analytics from this
  // pod's own cost_event telemetry (total $ ÷ token volume), distinct from
  // the categorical cost-band shown on the AI Optimization tier rows. Rows
  // below the min-sample threshold carry low_confidence and render the rate
  // as "—" with an "insufficient data" hover so a lightly-used model doesn't
  // show a noisy number.
  const modelEl = document.getElementById('usage-table-model');
  if (modelEl) {
    const rows = d.by_model || [];
    if (!rows.length) { modelEl.innerHTML = '<div class="empty">No data</div>'; }
    else {
      modelEl.innerHTML = '<div class="resp-table-wrap"><table class="resp-table resp-table-dense"><thead><tr><th></th><th>Model</th><th>Calls</th><th>Cost</th><th>Cost/turn</th><th title="Observed $ per 1k input tokens — total spend (incl. cache cost) ÷ input volume">$/1k in</th><th title="Observed $ per 1k output tokens — total spend (incl. cache cost) ÷ output volume">$/1k out</th><th data-secondary>Tokens (in/out)</th><th data-secondary>Cache read</th><th data-secondary>Cache write</th><th data-secondary>Auth</th></tr></thead><tbody>' +
        rows.map(r => {
          const color = r.color || _usageModelColor(r.model);
          const isMax = r.auth_mode === 'MAX';
          const authBadge = isMax
            ? `<span style="color:var(--green);font-size:0.72rem;font-weight:600">MAX</span>`
            : `<span style="color:var(--yellow);font-size:0.72rem;font-weight:600">API key</span>`;
          const modelShort = r.model.replace(':api_key','').split('/').pop();
          const apiKeySuffix = r.model.endsWith(':api_key') ? ' <span style="color:var(--text3);font-size:0.68rem">[key]</span>' : '';
          const costPerTurn = (r.calls > 0 && r.cost > 0) ? (r.cost / r.calls) : 0;
          const perKIn  = _usagePer1kCell(r.usd_per_1k_input,  r.low_confidence);
          const perKOut = _usagePer1kCell(r.usd_per_1k_output, r.low_confidence);
          return `<tr>
            <td data-label="" style="width:12px"><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${color}"></span></td>
            <td data-label="Model" style="font-family:monospace;font-size:0.75rem;color:var(--teal)" title="${escHtml(r.model)}">${escHtml(modelShort)}${apiKeySuffix}</td>
            <td data-label="Calls" style="font-size:0.78rem">${r.calls.toLocaleString()}</td>
            <td data-label="Cost" style="font-size:0.78rem">${r.cost > 0 ? _usageFmt$(r.cost) : '—'}</td>
            <td data-label="Cost/turn" style="font-size:0.78rem;color:var(--text2)">${costPerTurn > 0 ? _usageFmt$(costPerTurn) : '—'}</td>
            <td data-label="$/1k in" style="font-size:0.78rem">${perKIn}</td>
            <td data-label="$/1k out" style="font-size:0.78rem">${perKOut}</td>
            <td data-label="Tokens (in/out)" data-secondary style="font-size:0.75rem;color:var(--text2)">${_fmtTok(r.input_tokens)} / ${_fmtTok(r.output_tokens)}</td>
            <td data-label="Cache read" data-secondary style="font-size:0.75rem;color:var(--text2)">${_fmtTok(r.cache_read)}</td>
            <td data-label="Cache write" data-secondary style="font-size:0.75rem;color:var(--text2)">${_fmtTok(r.cache_write)}</td>
            <td data-label="Auth" data-secondary>${authBadge}</td>
          </tr>`;
        }).join('') + '</tbody></table></div>';
    }
  }

  // Model × Audience table — per-model split between Human (direct
  // operator/user input) and Non-Human (heartbeat/cron/subagent/forge/
  // unknown). Lets the operator spot premium models doing autonomous
  // work without cross-referencing By Model and By Source tables.
  const audienceEl = document.getElementById('usage-table-model-audience');
  if (audienceEl) {
    const rows = d.by_model_by_audience || [];
    if (!rows.length) {
      audienceEl.innerHTML = '<div class="empty">No data</div>';
    } else {
      const totalCost = rows.reduce((a, r) => a + (r.total_cost || 0), 0);
      let html = '<div class="resp-table-wrap"><table class="resp-table resp-table-dense">' +
        '<thead><tr>' +
        '<th></th>' +
        '<th>Model</th>' +
        '<th colspan="2" style="text-align:center;border-left:1px solid var(--border)">Human</th>' +
        '<th data-secondary style="text-align:center">H · $/turn</th>' +
        '<th colspan="2" style="text-align:center;border-left:1px solid var(--border)">Non-Human</th>' +
        '<th data-secondary style="text-align:center">NH · $/turn</th>' +
        '<th style="border-left:1px solid var(--border)">Total cost</th>' +
        '<th>Human %</th>' +
        '</tr><tr style="font-size:0.7rem;color:var(--text3)">' +
        '<th></th><th></th>' +
        '<th style="border-left:1px solid var(--border)">Turns</th><th>Cost</th>' +
        '<th data-secondary></th>' +
        '<th style="border-left:1px solid var(--border)">Turns</th><th>Cost</th>' +
        '<th data-secondary></th>' +
        '<th style="border-left:1px solid var(--border)"></th>' +
        '<th></th>' +
        '</tr></thead><tbody>';
      let totalHumanCalls = 0, totalNonHumanCalls = 0;
      let totalHumanCost = 0, totalNonHumanCost = 0;
      for (const r of rows) {
        const color = r.color || _usageModelColor(r.model);
        const modelShort = (r.model || '').replace(':api_key', '').split('/').pop();
        const apiKeySuffix = (r.model || '').endsWith(':api_key')
          ? ' <span style="color:var(--text3);font-size:0.68rem">[key]</span>'
          : '';
        const human = r.human || {calls: 0, cost: 0};
        const nonHuman = r.non_human || {calls: 0, cost: 0};
        const humanCostPerTurn = (human.calls > 0 && human.cost > 0)
          ? (human.cost / human.calls) : 0;
        const nonHumanCostPerTurn = (nonHuman.calls > 0 && nonHuman.cost > 0)
          ? (nonHuman.cost / nonHuman.calls) : 0;
        const humanShare = (r.total_cost > 0)
          ? (human.cost / r.total_cost * 100) : 0;
        totalHumanCalls += human.calls;
        totalNonHumanCalls += nonHuman.calls;
        totalHumanCost += human.cost;
        totalNonHumanCost += nonHuman.cost;
        html += `<tr>
          <td data-label="" style="width:12px"><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${color}"></span></td>
          <td data-label="Model" style="font-family:monospace;font-size:0.75rem;color:var(--teal)" title="${escHtml(r.model || '')}">${escHtml(modelShort)}${apiKeySuffix}</td>
          <td data-label="H · Turns" style="font-size:0.78rem;border-left:1px solid var(--border)">${human.calls.toLocaleString()}</td>
          <td data-label="H · Cost" style="font-size:0.78rem">${human.cost > 0 ? _usageFmt$(human.cost) : '—'}</td>
          <td data-label="H · $/turn" data-secondary style="font-size:0.75rem;color:var(--text2)">${humanCostPerTurn > 0 ? _usageFmt$(humanCostPerTurn) : '—'}</td>
          <td data-label="NH · Turns" style="font-size:0.78rem;border-left:1px solid var(--border)">${nonHuman.calls.toLocaleString()}</td>
          <td data-label="NH · Cost" style="font-size:0.78rem">${nonHuman.cost > 0 ? _usageFmt$(nonHuman.cost) : '—'}</td>
          <td data-label="NH · $/turn" data-secondary style="font-size:0.75rem;color:var(--text2)">${nonHumanCostPerTurn > 0 ? _usageFmt$(nonHumanCostPerTurn) : '—'}</td>
          <td data-label="Total cost" style="font-size:0.78rem;border-left:1px solid var(--border);font-weight:600">${r.total_cost > 0 ? _usageFmt$(r.total_cost) : '—'}</td>
          <td data-label="Human %" style="font-size:0.78rem;color:var(--text2)">${r.total_cost > 0 ? humanShare.toFixed(0) + '%' : '—'}</td>
        </tr>`;
      }
      // Totals row — gives the operator the headline split at the
      // bottom so they don't have to mentally sum the columns.
      const totalHumanShare = (totalCost > 0)
        ? (totalHumanCost / totalCost * 100) : 0;
      html += `<tr style="border-top:2px solid var(--border);font-weight:600;background:rgba(255,255,255,0.02)">
        <td></td>
        <td data-label="Model" style="font-size:0.78rem;color:var(--text2)">All models</td>
        <td data-label="H · Turns" style="font-size:0.78rem;border-left:1px solid var(--border)">${totalHumanCalls.toLocaleString()}</td>
        <td data-label="H · Cost" style="font-size:0.78rem">${totalHumanCost > 0 ? _usageFmt$(totalHumanCost) : '—'}</td>
        <td data-label="H · $/turn" data-secondary style="font-size:0.78rem;color:var(--text2)">${totalHumanCalls > 0 && totalHumanCost > 0 ? _usageFmt$(totalHumanCost / totalHumanCalls) : '—'}</td>
        <td data-label="NH · Turns" style="font-size:0.78rem;border-left:1px solid var(--border)">${totalNonHumanCalls.toLocaleString()}</td>
        <td data-label="NH · Cost" style="font-size:0.78rem">${totalNonHumanCost > 0 ? _usageFmt$(totalNonHumanCost) : '—'}</td>
        <td data-label="NH · $/turn" data-secondary style="font-size:0.78rem;color:var(--text2)">${totalNonHumanCalls > 0 && totalNonHumanCost > 0 ? _usageFmt$(totalNonHumanCost / totalNonHumanCalls) : '—'}</td>
        <td data-label="Total cost" style="font-size:0.78rem;border-left:1px solid var(--border)">${totalCost > 0 ? _usageFmt$(totalCost) : '—'}</td>
        <td data-label="Human %" style="font-size:0.78rem;color:var(--text2)">${totalCost > 0 ? totalHumanShare.toFixed(0) + '%' : '—'}</td>
      </tr>`;
      html += '</tbody></table></div>';
      audienceEl.innerHTML = html;
    }
  }

  // By Channel table — one row per real conversation (thread sub-rows rolled
  // up into an expandable child list), then a grouped "System & scheduled"
  // block for non-conversation volume (heartbeat / forge / subagents / …). The
  // backend tags each row {system, category, threads[], label, is_dm}; a DM
  // resolves to "DM · <name>" when the cache already carries the participant.
  const chanEl = document.getElementById('usage-table-channel');
  if (chanEl) {
    const allRows = d.by_channel || [];
    const realRows = allRows.filter(r => !r.system);
    const sysRows = allRows.filter(r => r.system);
    if (!allRows.length) { chanEl.innerHTML = '<div class="empty">No data</div>'; }
    else {
      let html = '<div class="resp-table-wrap"><table class="resp-table resp-table-dense"><thead><tr><th>Channel</th><th>Calls</th><th>Cost</th><th data-secondary>Cost/turn</th></tr></thead><tbody>';
      realRows.forEach((r, idx) => {
        const costPerTurn = (r.calls > 0 && r.cost > 0) ? (r.cost / r.calls) : 0;
        const label = r.label || r.channel;
        const threads = r.threads || [];
        const hasThreads = threads.length > 0;
        const icon = hasThreads
          ? '<span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>'
          : '';
        const threadNote = hasThreads
          ? ` <span style="color:var(--text2);font-size:0.72rem">· ${threads.length} thread${threads.length === 1 ? '' : 's'}</span>`
          : '';
        // Raw conversation id as a dim mono secondary line, only when the
        // primary label resolved to something friendlier (e.g. "DM · Greg").
        const idLine = (r.label && r.label !== r.channel)
          ? `<div style="font-size:0.72rem;color:var(--text2);font-family:monospace" title="${escHtml(r.channel)}">${escHtml(r.channel)}</div>`
          : '';
        html += `<tr class="usage-chan-row" data-chan="${idx}"${hasThreads ? ` onclick="toggleUsageChanThreads(${idx})" style="cursor:pointer"` : ''}>
          <td data-label="Channel" style="font-size:0.82rem">${icon}${escHtml(label)}${threadNote}${idLine}</td>
          <td data-label="Calls" style="font-size:0.78rem">${r.calls.toLocaleString()}</td>
          <td data-label="Cost" style="font-size:0.78rem">${r.cost > 0 ? _usageFmt$(r.cost) : '—'}</td>
          <td data-label="Cost/turn" data-secondary style="font-size:0.78rem;color:var(--text2)">${costPerTurn > 0 ? _usageFmt$(costPerTurn) : '—'}</td>
        </tr>`;
        threads.forEach(th => {
          const tcpt = (th.calls > 0 && th.cost > 0) ? (th.cost / th.calls) : 0;
          html += `<tr class="usage-chan-thread" data-chan="${idx}" hidden>
            <td data-label="Channel" style="font-size:0.78rem;color:var(--text2);padding-left:22px">↳ thread ${escHtml(_usageThreadLabel(th.thread_ts))}</td>
            <td data-label="Calls" style="font-size:0.78rem;color:var(--text2)">${(th.calls || 0).toLocaleString()}</td>
            <td data-label="Cost" style="font-size:0.78rem;color:var(--text2)">${th.cost > 0 ? _usageFmt$(th.cost) : '—'}</td>
            <td data-label="Cost/turn" data-secondary style="font-size:0.78rem;color:var(--text2)">${tcpt > 0 ? _usageFmt$(tcpt) : '—'}</td>
          </tr>`;
        });
      });
      if (sysRows.length) {
        html += '<tr class="usage-chan-sys-head"><td colspan="4" style="font-size:0.72rem;color:var(--text2);padding-top:8px;border-top:1px solid var(--border)">System &amp; scheduled — no conversation</td></tr>';
        for (const r of sysRows) {
          const costPerTurn = (r.calls > 0 && r.cost > 0) ? (r.cost / r.calls) : 0;
          html += `<tr class="usage-chan-sys">
            <td data-label="Channel" style="font-size:0.82rem;color:var(--text2)">${escHtml(r.category || r.channel)}</td>
            <td data-label="Calls" style="font-size:0.78rem;color:var(--text2)">${r.calls.toLocaleString()}</td>
            <td data-label="Cost" style="font-size:0.78rem;color:var(--text2)">${r.cost > 0 ? _usageFmt$(r.cost) : '—'}</td>
            <td data-label="Cost/turn" data-secondary style="font-size:0.78rem;color:var(--text2)">${costPerTurn > 0 ? _usageFmt$(costPerTurn) : '—'}</td>
          </tr>`;
        }
      }
      html += '</tbody></table></div>';
      chanEl.innerHTML = html;
    }
  }

  // By Source table — hierarchical: bucket header rows with the underlying
  // source values indented under each. Bucket totals match the Activity
  // Composition card; sub-rows show what's actually inside each bucket.
  const srcEl = document.getElementById('usage-table-source');
  if (srcEl) {
    const rows = d.by_source || [];
    if (!rows.length) {
      srcEl.innerHTML = '<div class="empty">No data</div>';
    } else {
      const grouped = { human: [], scheduled: [], background: [] };
      for (const r of rows) grouped[usageBucketOf(r.source)].push(r);
      const totalCalls = rows.reduce((a, r) => a + (r.calls || 0), 0);
      let html = '<div class="resp-table-wrap"><table class="resp-table resp-table-dense"><thead><tr><th>Source</th><th>Turns</th><th data-secondary>Sessions</th><th>Cost</th><th data-secondary>Cost/turn</th><th>%</th></tr></thead><tbody>';
      for (const bucket of USAGE_BUCKETS_ORDERED) {
        const items = grouped[bucket];
        if (!items.length) continue;
        const bCalls    = items.reduce((a, r) => a + (r.calls    || 0), 0);
        const bSessions = items.reduce((a, r) => a + (r.sessions || 0), 0);
        const bCost     = items.reduce((a, r) => a + (r.cost     || 0), 0);
        const bPct      = totalCalls > 0 ? (bCalls / totalCalls * 100) : 0;
        const bCostPerTurn = (bCalls > 0 && bCost > 0) ? (bCost / bCalls) : 0;
        // Single-item buckets: collapse to one row using the bucket label
        // (the sub-row would otherwise be a redundant duplicate of the
        // bucket totals). Multi-item buckets keep the header + sub-rows
        // so the operator can see what's inside (e.g. heartbeat vs cron).
        if (items.length === 1) {
          html += `<tr class="usage-source-bucket">
            <td data-label="Source"><span class="usage-comp-swatch" style="background:${USAGE_BUCKET_COLORS[bucket]}"></span><strong>${USAGE_BUCKET_LABELS[bucket]}</strong></td>
            <td data-label="Turns"><strong>${bCalls.toLocaleString()}</strong></td>
            <td data-label="Sessions" data-secondary><strong>${bSessions.toLocaleString()}</strong></td>
            <td data-label="Cost"><strong>${bCost > 0 ? _usageFmt$(bCost) : '—'}</strong></td>
            <td data-label="Cost/turn" data-secondary><strong>${bCostPerTurn > 0 ? _usageFmt$(bCostPerTurn) : '—'}</strong></td>
            <td data-label="%"><strong>${bPct.toFixed(0)}%</strong></td>
          </tr>`;
          continue;
        }
        html += `<tr class="usage-source-bucket">
          <td data-label="Source"><span class="usage-comp-swatch" style="background:${USAGE_BUCKET_COLORS[bucket]}"></span><strong>${USAGE_BUCKET_LABELS[bucket]}</strong></td>
          <td data-label="Turns"><strong>${bCalls.toLocaleString()}</strong></td>
          <td data-label="Sessions" data-secondary><strong>${bSessions.toLocaleString()}</strong></td>
          <td data-label="Cost"><strong>${bCost > 0 ? _usageFmt$(bCost) : '—'}</strong></td>
          <td data-label="Cost/turn" data-secondary><strong>${bCostPerTurn > 0 ? _usageFmt$(bCostPerTurn) : '—'}</strong></td>
          <td data-label="%"><strong>${bPct.toFixed(0)}%</strong></td>
        </tr>`;
        for (const r of items) {
          const pct = totalCalls > 0 ? (r.calls / totalCalls * 100) : 0;
          const costPerTurn = (r.calls > 0 && r.cost > 0) ? (r.cost / r.calls) : 0;
          html += `<tr class="usage-source-sub">
            <td data-label="Source">↳ ${escHtml(r.source || 'unknown')}</td>
            <td data-label="Turns">${(r.calls || 0).toLocaleString()}</td>
            <td data-label="Sessions" data-secondary>${(r.sessions || 0).toLocaleString()}</td>
            <td data-label="Cost">${r.cost > 0 ? _usageFmt$(r.cost) : '—'}</td>
            <td data-label="Cost/turn" data-secondary>${costPerTurn > 0 ? _usageFmt$(costPerTurn) : '—'}</td>
            <td data-label="%">${pct.toFixed(0)}%</td>
          </tr>`;
        }
      }
      html += '</tbody></table></div>';
      srcEl.innerHTML = html;
    }
  }

  // By User table (top 10) — REAL PEOPLE only (system/scheduled traffic is
  // excluded upstream). The backend resolves a display_name (cache-only) and a
  // categorized fallback label; we show the name as primary text and the raw
  // platform id as a dim mono secondary line (+ title tooltip) for debugging.
  // A pod-wide "not admitted" badge flags people admitted to NO bot anywhere.
  const userEl = document.getElementById('usage-table-user');
  if (userEl) {
    const rows = (d.by_user || []).slice(0, 10);
    if (!rows.length) { userEl.innerHTML = '<div class="empty">No data</div>'; }
    else {
      userEl.innerHTML = '<div class="resp-table-wrap"><table class="resp-table resp-table-dense"><thead><tr><th>User</th><th>Calls</th></tr></thead><tbody>' +
        rows.map(r => {
          const label = r.label || r.display_name || r.user_id || '—';
          const rawId = r.user_id || '';
          // Show the raw id only when it adds info beyond the primary label
          // (it doesn't when the label already IS the id-based fallback).
          const idLine = (rawId && rawId !== '?' && rawId !== label)
            ? `<div style="font-size:0.72rem;color:var(--text2);font-family:monospace" title="${escHtml(rawId)}">${escHtml(rawId)}</div>`
            : '';
          // Pod-wide admission marker (semantic warn — attention, not category).
          const marker = r.unadmitted
            ? ` <span class="badge badge-sm badge-warn" title="Not admitted to any bot in this pod">not admitted</span>`
            : '';
          return `<tr>
            <td data-label="User" style="font-size:0.82rem">${escHtml(label)}${marker}${idLine}</td>
            <td data-label="Calls" style="font-size:0.78rem">${r.calls.toLocaleString()}</td>
          </tr>`;
        }).join('') + '</tbody></table></div>';
    }
  }

  // Re-apply persisted "Show more columns" state to the freshly-rendered
  // tables. The toggle buttons live in the static panel headers; the
  // .resp-table elements are recreated on each refresh, so the attribute
  // must be re-set after every render.
  _respColsApplyAll();
}

// ── By App (AL-1.3) ────────────────────────────────────────────────────────
//
// Per-app usage from the attribution rollup ({shared}/{bot}/usage-by-app.json),
// NOT from file mtimes. Three rules the table must never break
// (internal/design-app-attribution-2026-08-15.md §3):
//
//   1. Grades are additive columns. "Turns" / "Cost" are the deterministic
//      total (scheduled + explicit); the inferred column sits beside them
//      with its own badge and is never summed in.
//   2. Unattributed is a ROW, not a rounding error. It carries the share of
//      turns and spend the pod could not attribute to any app — the number
//      that says how much to trust every row above it.
//   3. "No rollup yet" renders as "not measured", never as $0.00.
//
// Window follows the page range: ≤1d → d1, ≤7d → d7, else d30. The card
// title states the window actually used so it can't misread as the range.

function _usageAppWindow(days) {
  if (days <= 1) return { key: 'd1', label: '24h' };
  if (days <= 7) return { key: 'd7', label: '7d' };
  return { key: 'd30', label: '30d' };
}

function _usageAppGradeBadge(kind, n) {
  if (!n) return '';
  const color = kind === 'scheduled' ? 'var(--cyan)' : 'var(--accent)';
  const label = kind === 'scheduled' ? 'scheduled' : 'explicit';
  return `<span class="badge" style="border-color:${color};color:${color}" title="${n} turn${n !== 1 ? 's' : ''} attributed via ${label} capture (deterministic)">${label} ${n}</span>`;
}

async function _renderUsageByApp() {
  const el = document.getElementById('usage-table-by-app');
  const titleEl = document.getElementById('usage-by-app-title');
  const noteEl = document.getElementById('usage-by-app-note');
  if (!el) return;

  const win = _usageAppWindow(_usageDays);
  const bot = _usageBot || 'all';
  if (titleEl) titleEl.textContent = `By App — last ${win.label}`;
  el.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';

  const qs = `window=${win.key}` + (bot === 'all' ? '' : `&bot=${encodeURIComponent(bot)}`);
  const d = await api('GET', `/api/analytics/usage/by-app?${qs}`);
  if (!d || d.error) {
    el.innerHTML = `<div class="empty">${escHtml(d?.error || 'Failed to load per-app usage.')}</div>`;
    if (noteEl) noteEl.innerHTML = '';
    return;
  }

  const bots = d.bots || {};
  const botIds = Object.keys(bots).sort();
  const showBotCol = botIds.length > 1;

  // Rows are (bot, app) pairs — an app id is per-bot, so merging the same
  // id across bots would fuse two separate installs into one fake row.
  const rows = [];
  let unTurns = 0, unCost = 0, unLegacy = 0, ovTurns = 0, ovCost = 0;
  let attributedTurns = 0, inferredTurns = 0, denomTurns = 0, attributedCost = 0;
  let measuredBots = 0;
  for (const b of botIds) {
    const entry = bots[b] || {};
    if (!entry.measured) continue;
    measuredBots++;
    for (const app of (entry.apps || [])) {
      rows.push({ bot: b, ...app });
      attributedCost += (app.total?.cost_estimated || 0) + (app.inferred?.cost_estimated || 0);
    }
    const un = entry.unattributed || {};
    unTurns += un.turns || 0;
    unCost += un.cost_estimated || 0;
    unLegacy += un.legacy_schema_turns || 0;
    const ov = entry.evolve_overhead || {};
    ovTurns += ov.turns || 0;
    ovCost += ov.cost_estimated || 0;
    const cov = entry.coverage || {};
    attributedTurns += cov.attributed_turns || 0;
    inferredTurns += cov.inferred_turns || 0;
    denomTurns += cov.app_turns_total || 0;
  }
  rows.sort((a, b) => ((b.total?.cost_estimated || 0) - (a.total?.cost_estimated || 0)));

  if (!measuredBots) {
    el.innerHTML = '<div class="empty">Per-app usage not measured yet — the daily rollup (03:35) has not written usage-by-app.json for '
      + (bot === 'all' ? 'any bot' : escHtml(bot)) + ' yet.</div>';
    if (noteEl) noteEl.innerHTML = '';
    return;
  }

  const head = '<div class="resp-table-wrap"><table class="resp-table resp-table-dense"><thead><tr>'
    + (showBotCol ? '<th>Bot</th>' : '')
    + '<th>App</th><th title="Turns attributed deterministically — scheduled + explicit">Turns</th>'
    + '<th>Cost</th><th>Attribution</th>'
    + '<th title="Turns a classifier GUESSED belonged to this app. Never added into the columns on the left.">Inferred</th>'
    + '<th data-secondary>Tokens (in/out)</th><th>Last seen</th>'
    + '</tr></thead><tbody>';

  const body = rows.map(r => {
    const total = r.total || {};
    const inf = r.inferred || {};
    const sched = (r.scheduled || {}).turns || 0;
    const expl = (r.explicit || {}).turns || 0;
    const infTurns = inf.turns || 0;
    const inferredCell = infTurns
      ? `<span class="badge" style="border-color:var(--text3);color:var(--text3)" title="Inferred by classifier — shown as a guess, never counted as fact">~${infTurns} · ${_usageFmt$(inf.cost_estimated || 0)}</span>`
      : '<span style="color:var(--text3)">—</span>';
    return `<tr>
      ${showBotCol ? `<td data-label="Bot" style="font-size:0.78rem;color:var(--text2)">${escHtml(r.bot)}</td>` : ''}
      <td data-label="App" style="font-family:monospace;font-size:0.75rem;color:var(--teal)">${escHtml(r.app_id)}</td>
      <td data-label="Turns" style="font-size:0.78rem">${(total.turns || 0).toLocaleString()}</td>
      <td data-label="Cost" style="font-size:0.78rem">${_usageFmt$(total.cost_estimated || 0)}</td>
      <td data-label="Attribution" style="font-size:0.78rem">${_usageAppGradeBadge('scheduled', sched)} ${_usageAppGradeBadge('explicit', expl)}</td>
      <td data-label="Inferred" style="font-size:0.78rem">${inferredCell}</td>
      <td data-label="Tokens (in/out)" data-secondary style="font-size:0.75rem;color:var(--text2)">${_fmtTok(total.input_tokens || 0)} / ${_fmtTok(total.output_tokens || 0)}</td>
      <td data-label="Last seen" style="font-size:0.75rem;color:var(--text2)">${r.last_seen_ts ? escHtml(fmtPodTimeFull(r.last_seen_ts) || r.last_seen_ts) : '—'}</td>
    </tr>`;
  }).join('');

  // The two rows that keep the table honest: everything the pod could not
  // attribute, and Evolve's own scaffolding (which is not an app at all).
  const span = showBotCol ? 3 : 2;
  const foot = `<tr style="border-top:1px solid var(--border)">
      <td data-label="App" colspan="${span}" style="font-size:0.78rem;color:var(--text2)">Unattributed <span style="color:var(--text3)">· no app signal on the turn</span></td>
      <td data-label="Cost" style="font-size:0.78rem;color:var(--text2)">${_usageFmt$(unCost)}</td>
      <td colspan="3" style="font-size:0.75rem;color:var(--text3)">${unTurns.toLocaleString()} turns${unLegacy ? ` · ${unLegacy.toLocaleString()} predate attribution` : ''}</td>
    </tr>
    <tr>
      <td data-label="App" colspan="${span}" style="font-size:0.78rem;color:var(--text3)">Evolve overhead <span style="color:var(--text3)">· summarizer / classifier / forge, not an app</span></td>
      <td data-label="Cost" style="font-size:0.78rem;color:var(--text3)">${_usageFmt$(ovCost)}</td>
      <td colspan="3" style="font-size:0.75rem;color:var(--text3)">${ovTurns.toLocaleString()} turns</td>
    </tr>`;

  el.innerHTML = head + body + foot + '</tbody></table></div>';

  if (noteEl) {
    const share = denomTurns ? (unTurns / denomTurns) : null;
    const covered = share == null ? null : (1 - share);
    const fallbackBots = d.usage_stats_fallback_bots || [];
    // Coverage is stated in BOTH units on purpose: a pod can attribute most
    // of its cheap turns and still leave most of its SPEND unattributed.
    const costDenom = attributedCost + unCost;
    const unCostPct = costDenom > 0 ? (unCost / costDenom * 100) : null;
    const costClause = unCostPct == null ? ''
      : ` Unattributed spend: <strong>${_usageFmt$(unCost)}</strong> of ${_usageFmt$(costDenom)} (${unCostPct.toFixed(1)}%).`;
    const covLine = covered == null
      ? 'No app-bearing turns in this window yet.'
      : `<strong style="color:${covered >= 0.5 ? 'var(--green)' : 'var(--orange)'}">${(covered * 100).toFixed(1)}%</strong> of turns in this window carried an app id (${attributedTurns.toLocaleString()} deterministic${inferredTurns ? `, ${inferredTurns.toLocaleString()} inferred` : ''}; ${unTurns.toLocaleString()} unattributed).${costClause}`;
    const fallbackLine = fallbackBots.length
      ? ` Still on the file-mtime fallback (no attributed turns yet): ${fallbackBots.map(escHtml).join(', ')}.`
      : '';
    noteEl.innerHTML = `<div style="font-size:0.78rem;color:var(--text2)">${covLine}${fallbackLine} Turns/Cost count only deterministic attribution (scheduled + explicit); inferred is shown separately and never added in.</div>`;
  }
}

function _renderContextHealth(d) {
  const el = document.getElementById('usage-context-health');
  if (!el) return;
  const ch = d.context_health;
  if (!ch || ch.session_count === 0) {
    el.innerHTML = '<div class="empty">No session data for context analysis.</div>';
    return;
  }

  const effPct = ch.cache_efficiency_pct || 0;
  const effColor = effPct >= 80 ? 'var(--green)' : effPct >= 60 ? 'var(--yellow)' : 'var(--red)';
  const effLabel = effPct >= 80 ? 'good' : effPct >= 60 ? 'moderate' : 'poor';

  el.innerHTML = `
    <div class="grid grid-4" style="margin-bottom:14px">
      <div class="stat-block"><div class="stat-value">${_fmtTok(ch.median_context)}</div><div class="stat-label">Median context</div></div>
      <div class="stat-block"><div class="stat-value">${_fmtTok(ch.p75_context)}</div><div class="stat-label">P75 context</div></div>
      <div class="stat-block"><div class="stat-value">${_fmtTok(ch.p95_context)}</div><div class="stat-label">P95 context</div></div>
      <div class="stat-block"><div class="stat-value">${_fmtTok(ch.max_context)}</div><div class="stat-label">Max context</div></div>
    </div>
    <div style="display:flex;gap:24px;align-items:flex-start;margin-bottom:14px;flex-wrap:wrap">
      <div>
        <div style="font-size:2rem;font-weight:700;color:${effColor};line-height:1">${effPct}%</div>
        <div style="font-size:0.75rem;color:var(--text2)">cache efficiency — ${effLabel}</div>
        <div style="font-size:0.72rem;color:var(--text3);margin-top:2px">cache_read / (cache_read + cache_write)</div>
      </div>
      <div style="font-size:0.82rem;color:var(--text2);padding-top:6px">
        <div>${ch.over_100k_count} session${ch.over_100k_count !== 1 ? 's' : ''} &gt; 100k tokens</div>
        <div>${ch.over_50k_count} session${ch.over_50k_count !== 1 ? 's' : ''} &gt; 50k tokens</div>
        <div style="margin-top:4px;color:var(--text3)">${ch.session_count} total sessions in period</div>
      </div>
    </div>
    ${ch.top_sessions.length ? `
    <div class="section-head" style="margin-top:0;margin-bottom:8px">Top Sessions by Cost</div>
    <div class="resp-table-wrap"><table class="resp-table">
      <thead><tr><th>Date</th><th>Context</th><th>Turns</th><th>Cost</th><th>Session ID</th></tr></thead>
      <tbody>${ch.top_sessions.map(s => `<tr>
        <td data-label="Date" style="font-size:0.78rem;color:var(--text2)">${escHtml(s.date||'—')}</td>
        <td data-label="Context" style="font-size:0.78rem">${_fmtTok(s.context)}</td>
        <td data-label="Turns" style="font-size:0.78rem">${s.turns}</td>
        <td data-label="Cost" style="font-size:0.78rem;color:${s.cost>1?'var(--red)':s.cost>0.1?'var(--yellow)':'var(--text2)'}">${s.cost > 0 ? _usageFmt$(s.cost) : '—'}</td>
        <td data-label="Session ID" style="font-family:monospace;font-size:0.72rem;color:var(--text3)">${escHtml(s.session_id)}</td>
      </tr>`).join('')}</tbody>
    </table></div>` : ''}`;
}

// ── Compaction settings on Usage page ─────────────────────────────────────────

let _usageCompactionBot = null;
let _usageCompactionData = {};

async function _usageLoadCompaction(botName) {
  const panel = document.getElementById('usage-compact-panel');
  const label = document.getElementById('usage-compact-bot-label');
  if (!panel) return;

  if (!botName || botName === '') {
    panel.innerHTML = '<div class="empty">Select a specific bot above to view compaction settings.</div>';
    if (label) label.textContent = '';
    return;
  }

  if (label) label.textContent = `(${botName})`;
  panel.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';
  _usageCompactionBot = botName;

  const data = await api('GET', `/api/bot/compaction?bot=${botName}`);
  _usageCompactionData = data || {};

  if (data.error || !Object.keys(data).length) {
    panel.innerHTML = `<div class="empty">${escHtml(data.error || 'No compaction config found for this bot.')}</div>`;
    return;
  }

  const mode = data.mode || data.type || 'safeguard';
  const reserve = data.reserveTokens ?? data.reserve_tokens ?? data.contextReserve ?? '';
  const modeDescs = {
    safeguard: 'Summarises only when context is nearly full — safest option.',
    auto:      'Summarises automatically as needed.',
    manual:    'Only compacts when explicitly triggered.',
    off:       'Compaction disabled — context grows until limit.',
  };

  panel.innerHTML = `
    <div style="display:flex;gap:24px;flex-wrap:wrap;align-items:flex-start;margin-bottom:12px">
      <div>
        <div style="font-size:0.72rem;color:var(--text2);margin-bottom:4px">Mode</div>
        <select id="usage-compact-mode" class="input-w-sm" style="background:var(--bg2);border:1px solid var(--border);color:var(--text1);border-radius:5px;padding:4px 8px;font-size:0.82rem" onchange="_usageCompactModeChanged()">
          <option value="safeguard"${mode==='safeguard'?' selected':''}>safeguard</option>
          <option value="auto"${mode==='auto'?' selected':''}>auto</option>
          <option value="manual"${mode==='manual'?' selected':''}>manual</option>
          <option value="off"${mode==='off'?' selected':''}>off</option>
        </select>
        <div id="usage-compact-mode-desc" style="font-size:0.72rem;color:var(--text3);margin-top:3px;max-width:220px">${escHtml(modeDescs[mode]||'')}</div>
      </div>
      <div>
        <div style="font-size:0.72rem;color:var(--text2);margin-bottom:4px">Reserve tokens</div>
        <input id="usage-compact-reserve" type="number" value="${reserve}" placeholder="e.g. 8000"
          style="width:100px;background:var(--bg2);border:1px solid var(--border);color:var(--text1);border-radius:5px;padding:4px 8px;font-size:0.82rem">
        <div style="font-size:0.72rem;color:var(--text3);margin-top:3px">Keep this many tokens free</div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <button class="btn btn-primary btn-sm" onclick="_usageCompactionSave()">Save</button>
      <span id="usage-compact-status" style="font-size:0.8rem"></span>
    </div>
    <details style="margin-top:10px;font-size:0.75rem;color:var(--text2)">
      <summary style="display:flex;align-items:center;gap:8px;cursor:pointer"><span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>Raw config</summary>
      <div style="margin-top:6px;font-family:monospace;white-space:pre-wrap;font-size:0.72rem">${escHtml(JSON.stringify(data, null, 2))}</div>
    </details>`;
}

function _usageCompactModeChanged() {
  const sel = document.getElementById('usage-compact-mode');
  const desc = document.getElementById('usage-compact-mode-desc');
  if (!sel || !desc) return;
  const modeDescs = {
    safeguard: 'Summarises only when context is nearly full — safest option.',
    auto:      'Summarises automatically as needed.',
    manual:    'Only compacts when explicitly triggered.',
    off:       'Compaction disabled — context grows until limit.',
  };
  desc.textContent = modeDescs[sel.value] || '';
}

async function _usageCompactionSave() {
  const modeSel    = document.getElementById('usage-compact-mode');
  const reserveEl  = document.getElementById('usage-compact-reserve');
  const statusEl   = document.getElementById('usage-compact-status');
  if (!_usageCompactionBot) return;

  const mode    = modeSel?.value;
  const reserve = reserveEl?.value ? parseInt(reserveEl.value, 10) : null;
  if (statusEl) statusEl.innerHTML = '<span style="color:var(--text2)">Saving…</span>';

  const saves = [
    mode ? api('POST', '/api/config/push', { key: 'agents.defaults.compaction.mode', value: mode, bots: [_usageCompactionBot] }) : Promise.resolve({ ok: true }),
    reserve != null ? api('POST', '/api/config/push', { key: 'agents.defaults.compaction.reserveTokens', value: reserve, bots: [_usageCompactionBot] }) : Promise.resolve({ ok: true }),
  ];
  const results = await Promise.all(saves);
  const err = results.find(r => r.error)?.error;
  if (err) {
    if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">✗ ${escHtml(err)}</span>`;
  } else {
    if (statusEl) statusEl.innerHTML = '<span style="color:var(--green)">✓ Saved</span>';
    setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 2500);
    toast('Compaction config saved', 'ok');
  }
}

async function _renderSpendAlert() {
  const spendEl = document.getElementById('cost-spend-alert');
  if (!spendEl) return;
  try {
    const d = await api('GET', '/api/analytics/cost?days=30');
    const sa = d.spend_alerts || {};
    const saThreshold = sa.current_threshold != null ? `$${sa.current_threshold.toFixed(2)}` : '$5.00';
    const saCount = sa.alerts_sent_today || 0;
    const saChecked = sa.last_checked ? (fmtPodTimeFull(sa.last_checked) || sa.last_checked) : 'never';
    spendEl.innerHTML = `
      <div style="display:flex;gap:24px;padding:4px 0;font-size:0.82rem">
        <div><span style="font-weight:600;color:${saCount>0?'var(--orange)':'var(--text2)'}">${saCount}</span><span style="color:var(--text2);margin-left:4px">alert${saCount!==1?'s':''} sent today</span></div>
        <div style="color:var(--text2)">Threshold: <span style="color:var(--text)">${saThreshold}</span></div>
        <div style="color:var(--text2)">Last checked: <span style="color:var(--text)">${saChecked}</span></div>
      </div>`;
  } catch(e) {
    spendEl.innerHTML = '<div class="empty" style="padding:4px 0">Spend alert data unavailable.</div>';
  }
}

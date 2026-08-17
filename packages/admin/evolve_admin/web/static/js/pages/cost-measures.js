// ════════════════════════════════════════════════════════════════════════
// Page: Cost Measures (Cost → Cost Measures subtab) + Cost Import Modal
//
// The Cost Measures page — per-bot cost-cap configuration, score,
// audit/runaway/spike detectors, and the heartbeat-quick-fix flow. The
// largest single-page extraction in the cleanup arc.
//
// State (top of the file):
//   _cmBot                — currently selected bot
//   _cmProposalsByBot     — open RSI Cost proposals per bot
//   _cmCurrentBotCaps     — last-loaded per-bot caps
//   _cmPodCaps            — last-loaded pod-wide caps
//   _cmCurrentSettings    — bot's current openclaw.json values (deep)
//
// Lookup tables:
//   _CM_GRADE_COLORS / _CM_GRADE_BG — A/B/C/D/F grade palettes
//   _CM_CAP_FIELDS — pod / per-bot caps field metadata
//   _CM_FOOTPRINT_COLORS — app-footprint chart palette
//   _CM_FIELDS — cost-settings field metadata
//
// Sub-clusters:
//
//   Proposals strip — _cmLoadProposals + _cmRenderBotProposalsStrip
//
//   Bot tiles + score + chips — loadCostMeasures + _cmRenderTile +
//     _cmRenderBotTabs + _cmSelectBot + _cmLoadScore + _cmRenderScore +
//     _cmLoadChips + _cmFieldLabel + _cmJumpToField
//
//   Levers (Cost & Caps card) — _cmFetchAndPaintLevers + _cmPaintLevers +
//     _cmGoToCostCaps + _cmLoadBotCaps / _cmRefreshBotCaps +
//     _cmLoadPodCaps / _cmRefreshPodCaps + _cmLoadPreflight +
//     _cmRefreshPreflight + _cmPreflightRateColor + _cmRenderPreflight +
//     _cmCapsValueFor + _cmCapsStateFor + _cmFormatUsd +
//     _cmRenderCapsMatrix + _cmCapsChipClick + _cmCapsCustomCommit +
//     _cmCapsWrite + _cmCapsSetStatus
//
//   Pod proposals — _cmRenderPodProposals
//
//   App footprint chart — _cmLoadAppFootprint + _cmFmtBytes +
//     _cmRenderAppFootprint
//
//   Settings matrix (heartbeat / batch / cache / context-window) —
//     _cmLoadSettings + _cmRefreshSettings + _cmGetByPath +
//     _cmCellDisplay + _cmValuesEqual + _cmBuildPatch +
//     _cmToggleSettingsRow + _cmToggleAllSettingsRows +
//     _cmToggleCapsRow + _cmToggleAllCapsRows
//
//   Heartbeat quick-fix — _cmParseEveryToSeconds +
//     _cmDetectBadHeartbeatCombo + _cmApplyQuickFix
//
//   Matrix renderer + cell editor — _cmRenderMatrix + _cmCellCustomCommit
//     + _cmCellClick + _cmRenderInlineError + _cmHeaderApply +
//     _cmOtherChange + _cmDeleteOtherProfile + _cmSaveCurrentAsProfile
//
//   Runaway + Audit + Spikes — _cmRefreshRunaway + _cmRenderRunaway +
//     _cmRefreshAudit + _cmRenderAudit + _cmRefreshSpikes +
//     _cmRenderAppRollup + _cmRenderTriggerRollup + _cmRenderSpikes
//
//   Cost Import Modal — openCostImportModal + closeCostImportModal +
//     submitCostImport. Pastes a CSV/JSON dump into the Cost API.
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - api(), toast(), escHtml(), botLabel() — core/
//   - openProposalDetail — pages/self-improvement.js
//   - loadStatus() (Overview) — refreshed after cap writes
//   - nav() / window.nav — core/router.js (cap-jump deep links)
// ════════════════════════════════════════════════════════════════════════


// ══════════════════════════════════════════════════════
// Cost Measures
// ══════════════════════════════════════════════════════

let _cmBot = '';   // currently selected bot for Cost Measures page

// Open cost-dimension proposals grouped by bot_id. Powers the badge on each
// bot tab + the per-bot strip below the tabs. Cached in module state so a
// bot-switch re-renders without re-fetching.
let _cmProposalsByBot = {};   // {bot_id: [proposal, ...]}

async function _cmLoadProposals() {
  try {
    const r = await fetch('/api/arbiter/proposals?include=pending,snoozed&dimension=cost');
    const j = await r.json();
    const byBot = {};
    for (const p of ((j && j.ok && j.proposals) || [])) {
      const b = p.bot_id;
      if (!b) continue;
      (byBot[b] = byBot[b] || []).push(p);
    }
    _cmProposalsByBot = byBot;
  } catch (_e) {
    _cmProposalsByBot = {};
  }
}

function _cmRenderBotProposalsStrip() {
  const el = document.getElementById('cm-bot-proposals-strip');
  if (!el) return;
  const list = (_cmBot && _cmProposalsByBot[_cmBot]) || [];
  if (!list.length) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  const TOP = 3;
  const top = list.slice(0, TOP);
  const moreCount = Math.max(0, list.length - top.length);
  const items = top.map(p => {
    const color = URGENCY_COLORS[p.urgency] || '#888';
    const summary = p.admin_surface_summary || p.problem || '(no title)';
    return `
      <div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:0.82rem">
        <span class="badge" style="background:${color};color:#000;font-size:0.68rem;padding:1px 5px;flex-shrink:0">${escHtml(p.urgency)}</span>
        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escHtml(summary)}">${escHtml(summary)}</span>
        <span style="font-size:0.72rem;color:#777;flex-shrink:0">${escHtml(p.generator_id || '')} · ${escHtml(ago(p.created_at))}</span>
        <button class="btn btn-ghost btn-sm" style="flex-shrink:0;font-size:0.72rem;padding:1px 8px" onclick="openProposalDetail('${escHtml(p.id)}')">→ Act</button>
      </div>`;
  }).join('');
  // Pick the "see all" destination by majority surface. Cost
  // dimension is mixed across generators (efficiency_hawk is
  // improvement; cache_ttl_tuner/cost_spike/budget_hawk live on
  // Alerts). If any non-improvement proposal is in the result, the
  // bulk link goes to Alerts where the bulk of them render; an
  // all-improvement result stays on Recommendations.
  const anyAlerts = list.some(p => p.surface && p.surface !== 'improvement');
  const seeAllSurface = anyAlerts ? 'cleanup' : 'improvement';
  const seeAllLabel = anyAlerts ? 'see all in Alerts' : 'see all in Recommendations';
  const openLabel = anyAlerts ? 'Open Alerts →' : 'Open in Recommendations →';
  const moreLine = moreCount > 0
    ? `<div style="font-size:0.76rem;color:#888;margin-top:4px">+${moreCount} more — <a href="#" onclick="jumpToProposals('cost','${escHtml(_cmBot)}', '${seeAllSurface}'); return false;" style="color:#7fc8ff">${seeAllLabel}</a></div>`
    : `<div style="font-size:0.76rem;color:#888;margin-top:4px"><a href="#" onclick="jumpToProposals('cost','${escHtml(_cmBot)}', '${seeAllSurface}'); return false;" style="color:#7fc8ff">${openLabel}</a></div>`;
  el.innerHTML = `
    <div class="card stripe-card is-info" style="padding:10px 14px">
      <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:6px;gap:12px">
        <div style="font-size:0.82rem;font-weight:600;color:#7fc8ff">⬦ Open cost proposals for ${escHtml(botLabel(_cmBot))} (${list.length})</div>
        <div style="font-size:0.72rem;color:#888">from the arbiter</div>
      </div>
      ${items}
      ${moreLine}
    </div>`;
  el.style.display = '';
}

// Grade colors
const _CM_GRADE_COLORS = { A: '#22c55e', B: '#06b6d4', C: '#eab308', D: '#f97316', F: '#ef4444' };
const _CM_GRADE_BG     = { A: 'rgba(34,197,94,0.12)', B: 'rgba(6,182,212,0.12)', C: 'rgba(234,179,8,0.12)', D: 'rgba(249,115,22,0.12)', F: 'rgba(239,68,68,0.12)' };

async function loadCostMeasures() {
  // Per-bot cost-dimension proposals — populates the count badges on each
  // bot tab, the per-bot strip, and the pod-view aggregate card. Fire and
  // forget so the rest of the page doesn't block on the arbiter.
  _cmLoadProposals().then(() => {
    _cmRenderBotTabs();
    _cmRenderBotProposalsStrip();
    _cmRenderPodProposals();
  });
  _cmRenderBotTabs();
  // Sync container visibility with _cmBot (persisted across page revisits).
  // Default _cmBot='' → POD view. No auto-select; pod view IS the landing.
  const podView = document.getElementById('cm-pod-view');
  const botView = document.getElementById('cm-bot-view');
  if (podView) podView.style.display = _cmBot === '' ? '' : 'none';
  if (botView) botView.style.display = _cmBot === '' ? 'none' : '';

  // Always-on (visible on every tab): chips and the tile row. The POD
  // caps card loads only on POD; per-bot caps only on a bot tab. Both
  // are fetched conditionally below so we don't repaint hidden bodies.
  const always = [
    _cmLoadChips(),
    _cmLoadTiles(),
  ];
  const podOnly = _cmBot === '' ? [_cmLoadPodCaps(), _cmLoadPreflight()] : [];
  const perBot = _cmBot === '' ? [] : [
    _cmLoadBotCaps(),
    _cmLoadScore(),
    _cmLoadAppFootprint(),
    _cmLoadSettings(),   // _cmLoadSettings pulls /api/cost-profiles too
    _cmRefreshRunaway(),
    _cmRefreshAudit(),
    _cmRefreshSpikes(),
  ];
  await Promise.all([...always, ...podOnly, ...perBot]);
}

// Pod-wide open cost proposals — rendered into #cm-pod-proposals-body on
// the POD tab. Aggregates _cmProposalsByBot (populated by _cmLoadProposals)
// across every bot, sorted by urgency then age. Mirrors the visual shape of
// the per-bot strip so the operator's eye doesn't have to retrain.
function _cmRenderPodProposals() {
  const el = document.getElementById('cm-pod-proposals-body');
  if (!el) return;
  // Same ordering as URGENCY_COLORS declaration — highest severity first.
  const URGENCY_RANK = {
    security_critical: 0, operational_urgent: 1, cost_alert: 2,
    substrate_warn: 3, improvement: 4, hygiene: 5, whimsy: 6,
  };
  const all = [];
  for (const [bot, list] of Object.entries(_cmProposalsByBot || {})) {
    for (const p of (list || [])) all.push(p);
  }
  if (!all.length) {
    el.innerHTML = `
      <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:6px">
        <div class="card-title" style="margin-bottom:0">Open cost proposals</div>
        <div class="subtle" style="font-size:0.78rem">pod-wide</div>
      </div>
      <div class="subtle" style="font-size:0.82rem">No open cost proposals across any bot. ✓</div>`;
    return;
  }
  all.sort((a, b) => {
    const ra = URGENCY_RANK[a.urgency] ?? 99;
    const rb = URGENCY_RANK[b.urgency] ?? 99;
    if (ra !== rb) return ra - rb;
    return String(b.created_at || '').localeCompare(String(a.created_at || ''));
  });
  const TOP = 5;
  const top = all.slice(0, TOP);
  const moreCount = Math.max(0, all.length - top.length);
  const rows = top.map(p => {
    const color = URGENCY_COLORS[p.urgency] || '#888';
    const summary = p.admin_surface_summary || p.problem || '(no title)';
    const botName = p.bot_id ? botLabel(p.bot_id) : '';
    return `
      <div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:0.82rem">
        <span class="badge" style="background:${color};color:#000;font-size:0.68rem;padding:1px 5px;flex-shrink:0">${escHtml(p.urgency || '')}</span>
        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escHtml(summary)}">${escHtml(summary)}</span>
        <span style="font-size:0.72rem;color:#777;flex-shrink:0">${escHtml(botName)} · ${escHtml(p.generator_id || '')} · ${escHtml(ago(p.created_at))}</span>
        <button class="btn btn-ghost btn-sm" style="flex-shrink:0;font-size:0.72rem;padding:1px 8px" onclick="openProposalDetail('${escHtml(p.id)}')">→ Act</button>
      </div>`;
  }).join('');
  const moreLine = moreCount > 0
    ? `<div style="font-size:0.76rem;color:#888;margin-top:4px">+${moreCount} more — <a href="#" onclick="jumpToProposals('cost'); return false;" style="color:#7fc8ff">see all in Recommendations</a></div>`
    : `<div style="font-size:0.76rem;color:#888;margin-top:4px"><a href="#" onclick="jumpToProposals('cost'); return false;" style="color:#7fc8ff">Open in Recommendations →</a></div>`;
  el.innerHTML = `
    <div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:6px">
      <div style="display:flex;align-items:baseline;gap:8px">
        <div class="card-title" style="margin-bottom:0">Open cost proposals</div>
        <div class="subtle" style="font-size:0.78rem">pod-wide (${all.length})</div>
      </div>
      <div style="font-size:0.72rem;color:#888">from the arbiter</div>
    </div>
    ${rows}
    ${moreLine}`;
}

// Per-bot grade tiles at the top of the Cost Optimization page.
// Fetches /api/cost-optimization/tiles and renders one tile per bot.
// Each tile: grade circle (A-F), spend headline + delta, tier-colored
// model-mix bar, chip row. Click → selects that bot's tab below
// (so a tile-driven flow mirrors the Usage page's tile-as-selector
// pattern, plus a one-stop scan for the "which bot needs attention"
// question that motivated the redesign).
async function _cmLoadTiles() {
  const el = document.getElementById('cm-bot-tiles');
  if (!el) return;
  el.innerHTML = `<div class="loading" style="grid-column:1/-1"><span class="spinner"></span> Loading…</div>`;
  let data;
  try {
    data = await api('GET', '/api/cost-optimization/tiles');
  } catch (e) {
    el.innerHTML = `<div style="grid-column:1/-1;color:var(--text2);font-size:0.78rem">Tile data unavailable: ${escHtml(String(e?.message || e || 'unknown error'))}</div>`;
    return;
  }
  const tilesIn = (data && data.tiles) || [];
  if (!tilesIn.length) {
    el.innerHTML = `<div style="grid-column:1/-1;color:var(--text2);font-size:0.78rem">No bots registered.</div>`;
    return;
  }
  // Reorder via the canonical pod-wide rule (orderedBotIds) so the
  // tile row matches the sidebar nav order: primary (evo) first,
  // then the rest alphabetically by display label. Without this the
  // server hands tiles back in network.json::bots iteration order,
  // which is undefined-ordered. orderedBotIds also handles the
  // primary-resolution fallback chain (status → network → role).
  const tilesById = Object.fromEntries(tilesIn.map(t => [t.bot_id, t]));
  // orderedBotIds takes a bots-shaped object; synthesize one from
  // the tiles so we don't depend on _statusData.bots having exactly
  // the same id set (a tile for a bot the status view doesn't carry
  // would otherwise be dropped). Pass each tile's bot_id as both
  // key and value so orderedBotIds's role lookup falls through to
  // alpha-sort, then re-introduce the primary if we have one.
  const synthBots = {};
  for (const id of Object.keys(tilesById)) {
    // If status data has a richer entry for this id, prefer it
    // (carries role, label, etc.). Fall back to a minimal stub.
    synthBots[id] = (_statusData?.bots?.[id]) || { id };
  }
  const ordered = orderedBotIds(synthBots);
  const tiles = ordered.map(id => tilesById[id]).filter(Boolean);
  el.innerHTML = tiles.map(t => _cmRenderTile(t)).join('');
  // Fan out per-bot customizations fetches in the background so the
  // cost-lever chips ("TTL: short / cap: none") on each tile reflect
  // live state. Tile-render doesn't wait — the placeholder values
  // ("TTL: —") show while the fetches resolve, and the Edit button
  // refetches on click so the modal is always authoritative even if
  // these background fetches haven't completed yet.
  _cmFetchAndPaintLevers(tiles.map(t => t.bot_id).filter(Boolean));
}

function _cmRenderTile(t) {
  const id = t.bot_id || '';
  const label = botLabel(id) || id;
  const grade = (t.grade || 'F').toUpperCase();
  const score = (typeof t.score === 'number') ? t.score : 0;
  const gradeClass = `cm-grade-${grade}`;
  const isActive = (id === _cmBot);
  const activeClass = isActive ? ' active' : '';

  // Spend headline — 28d with delta vs prior 28d, matching Usage tile.
  const spend = t.spend || {};
  const usd28 = (typeof spend.usd_28d === 'number') ? spend.usd_28d : 0;
  const deltaPct = (typeof spend.delta_pct_28d === 'number') ? spend.delta_pct_28d : null;
  let deltaHtml = '';
  if (deltaPct === null) {
    deltaHtml = usd28 > 0 ? `<span class="pod-trend-up">▲ new</span>` : '';
  } else if (Math.abs(deltaPct) < 5) {
    deltaHtml = `<span class="pod-trend-flat">≈ flat</span>`;
  } else if (deltaPct > 0) {
    deltaHtml = `<span class="pod-trend-up">▲ ${Math.round(deltaPct)}% vs prior 28d</span>`;
  } else {
    deltaHtml = `<span class="pod-trend-down">▼ ${Math.round(Math.abs(deltaPct))}% vs prior 28d</span>`;
  }

  // Tier-colored mix bar. Each tier segment width is proportional to
  // turn_share so the visual story is "more green = more tier3-routed,
  // regardless of provider." Empty mix renders as a gray "no data" bar
  // so an idle bot doesn't fake 0% routing health.
  const mix = t.model_mix || {};
  const byTier = Array.isArray(mix.by_tier) ? mix.by_tier : [];
  const totalTurns = mix.total_turns || 0;
  let mixHtml;
  let mixLabel;
  if (totalTurns === 0) {
    mixHtml = `<div class="cm-mix-bar cm-mix-bar-empty">no turns 7d</div>`;
    mixLabel = '';
  } else {
    // Order tiers consistently inside each bar (tier3 → tier2 → tier1
    // → premium → tier0 → unknown) regardless of share size — same color
    // in the same position across all bots makes the row instantly
    // readable. ``premium`` (Fable-class frontier) sits just past tier1
    // (opus) so the most-expensive spend is at the right edge of the bar.
    const order = ['tier3', 'tier2', 'tier1', 'premium', 'tier0', 'unknown'];
    const sorted = order
      .map(tier => byTier.find(b => b.tier === tier))
      .filter(Boolean);
    const segs = sorted.map(b => {
      const pct = Math.max(0.5, (b.turn_share || 0) * 100); // min 0.5% so tiny slivers still visible
      const tierClass = `cm-mix-seg-${b.tier}`;
      const dom = b.dominant_model ? b.dominant_model.split('/').pop() : b.tier;
      const turns = b.turns || 0;
      const sharePct = Math.round((b.turn_share || 0) * 100);
      const tip = `${b.tier}: ${turns} turns (${sharePct}%) — ${dom}`;
      return `<div class="${tierClass}" style="width:${pct}%" title="${escHtml(tip)}"></div>`;
    }).join('');
    mixHtml = `<div class="cm-mix-bar">${segs}</div>`;
    // Label below the bar — names the dominant tier so the operator
    // can verify a green-heavy bar IS tier3 (not all tier1 power if
    // the bot is configured that way). Drives the same vocabulary
    // primary_model_floor_advisor uses.
    const top = sorted[0];
    if (top) {
      const sharePct = Math.round((top.turn_share || 0) * 100);
      const dom = top.dominant_model ? top.dominant_model.split('/').pop() : top.tier;
      mixLabel = `<div class="cm-bot-tile-mix-label">${escHtml(dom)} ${sharePct}%</div>`;
    } else {
      mixLabel = '';
    }
  }

  // Chips — already prioritized + truncated server-side. We just render.
  const chips = Array.isArray(t.chips) ? t.chips : [];
  const chipHtml = chips.length
    ? `<div class="cm-bot-tile-chips">${chips.map(ch => {
        const sev = ch.severity || 'info';
        const _sevToBadge = { critical: 'badge-crit', warn: 'badge-warn', info: 'badge-ok' };
        const cls = `badge badge-sm ${_sevToBadge[sev] || 'badge-neutral'}`;
        // The cascade chip is the only chip with a useful click target —
        // it deep-links to the AI Optimization → Cascade Routing card for
        // this bot, where the operator can flip the toggle. Other chips
        // (cost spike, breaker tripped, etc.) are passive status badges;
        // a click on those just falls through to the tile's own onclick
        // which selects the bot tab below.
        const isCascade = ch.id === 'cascade_live';
        const clickAttrs = isCascade
          ? ` style="cursor:pointer" onclick="event.stopPropagation();_aiJumpToCascadeForBot('${escHtml(id)}')"`
          : '';
        const title = isCascade
          ? 'Cascade routing on — click to manage on AI Optimization'
          : (ch.detail || ch.label || ch.id || '');
        const text = ch.label || ch.id || '';
        return `<span class="${cls}" title="${escHtml(title)}"${clickAttrs}>${escHtml(text)}</span>`;
      }).join('')}</div>`
    : `<div class="cm-bot-tile-chips"></div>`;

  const titleAttr = `${label} — Cost Optimization grade ${grade} (${score}/100)`;

  // Cost & caps footer — glanceable remediation-ladder chips. Phase 7
  // of the 2026-06 cost-cap normalization promoted these to show the
  // three escalating remediation tiers (tier_downgrade → L1 → L2) so
  // the operator can see the full ladder at a glance. The session cap
  // is the sentinel (in-flight rejection) and gets a separate chip.
  // Chips populate via _cmFetchAndPaintLevers after the tile mounts.
  const leversHtml = `<div class="cm-bot-tile-levers" data-bot="${escHtml(id)}"
       style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:4px;padding-top:6px;border-top:1px solid var(--border);font-size:0.7rem">
    <span id="cm-lever-tier-${escHtml(id)}" class="cm-lever-chip" title="Tier-downgrade threshold — switches primary to tier-3 model for the day"
          style="display:inline-block;padding:1px 7px;border-radius:10px;background:var(--bg3);color:var(--text3);border:1px solid var(--border);cursor:pointer"
          onclick="event.stopPropagation();_cmGoToCostCaps('${escHtml(id)}')">tier: —</span>
    <span id="cm-lever-dailycap-${escHtml(id)}" class="cm-lever-chip" title="L1 circuit breaker (daily) — stops auto sessions, chat works"
          style="display:inline-block;padding:1px 7px;border-radius:10px;background:var(--bg3);color:var(--text3);border:1px solid var(--border);cursor:pointer"
          onclick="event.stopPropagation();_cmGoToCostCaps('${escHtml(id)}')">L1: —</span>
    <span id="cm-lever-l2-${escHtml(id)}" class="cm-lever-chip" title="L2 circuit breaker (daily) — shuts down gateway, manual reset only"
          style="display:inline-block;padding:1px 7px;border-radius:10px;background:var(--bg3);color:var(--text3);border:1px solid var(--border);cursor:pointer"
          onclick="event.stopPropagation();_cmGoToCostCaps('${escHtml(id)}')">L2: —</span>
    <span id="cm-lever-budget-${escHtml(id)}" class="cm-lever-chip" title="Per-session cost cap — rejects next turn in any over-cap session"
          style="display:inline-block;padding:1px 7px;border-radius:10px;background:var(--bg3);color:var(--text3);border:1px solid var(--border);cursor:pointer"
          onclick="event.stopPropagation();_cmGoToCostCaps('${escHtml(id)}')">session: —</span>
    <!-- The Configure → button was removed in PR G. The whole tile is
         already clickable (top-level onclick → _cmSelectBot), the matrices
         that the button used to deep-link to sit immediately below the
         tile row on the same page, and the button visibly jumped during
         the post-fetch re-paint of the lever chips. Three reasons to
         delete it; no reason to keep it. -->
  </div>`;

  return `<div class="cm-bot-tile${activeClass}" data-bot="${escHtml(id)}" onclick="_cmSelectBot('${escHtml(id)}')" title="${escHtml(titleAttr)}">
    <div class="cm-bot-tile-header">
      <div class="cm-bot-tile-name">${escHtml(label)}</div>
      <div class="cm-bot-tile-grade">
        <div class="cm-bot-tile-grade-circle ${gradeClass}">${escHtml(grade)}</div>
        <div class="cm-bot-tile-score-detail">${score}<br>/ 100</div>
      </div>
    </div>
    <div>
      <div class="cm-bot-tile-spend">$${usd28.toFixed(2)}</div>
      <div class="cm-bot-tile-delta">${deltaHtml}</div>
    </div>
    ${mixHtml}
    ${mixLabel}
    ${chipHtml}
    ${leversHtml}
  </div>`;
}

// Fetch each tile's bot-setup payload and paint the cost-lever chips.
// Fan-out parallel so a slow bot doesn't gate the rest. Phase 4b made
// /api/arbiter/bot-setup the single source for the per-bot cost knobs
// (session cap, daily hard cap); the Customizations card's TTL +
// per-session pickers were retired.
async function _cmFetchAndPaintLevers(botIds) {
  // Phase 3 normalization: chips read from the canonical
  // /api/arbiter/bot-setup endpoint (BE config-backed) instead of two
  // separate stores (customizations + network.json). Daily hard cap +
  // per-session cap both come from the same payload.
  if (!Array.isArray(botIds) || botIds.length === 0) return;
  await Promise.all(botIds.map(async (botId) => {
    try {
      const r = await fetch(`/api/arbiter/bot-setup/${encodeURIComponent(botId)}`);
      const j = await r.json();
      if (!j || !j.ok) return;
      _cmPaintLevers(botId, j);
    } catch (_) {
      // Silent — a per-bot 404 or transient error just leaves the chip
      // at its "—" placeholder. The Configure → link still works.
    }
  }));
}

function _cmPaintLevers(botId, setup) {
  // Show the *effective* threshold on each chip — the one that'll actually
  // trip — not just the explicit per-bot override. Resolution per the caps
  // spec (docs/spec-cost-caps-2026-06-05.md §"Resolution"):
  //   - explicit > 0       → use the override
  //   - explicit === 0     → operator explicitly disabled (matrix "Off"); show "off"
  //   - explicit === null  → inherit pod default; if pod > 0 show that, else "off"
  // Before this fix the tile chips showed "off" for any bot that had no
  // explicit override, which was misleading whenever the pod default was
  // active. L1 was already correct via effective_daily_hard_usd; this
  // brings tier / L2 / session to parity.
  function _resolveEffective(explicit, podDefault) {
    if (typeof explicit === 'number' && explicit > 0) return explicit;
    if (explicit === 0) return null;  // matrix "Off" chip = explicit disable
    // explicit is null/undefined → inherit pod default
    if (typeof podDefault === 'number' && podDefault > 0) return podDefault;
    return null;
  }

  function _setChip(el, label, value, opts) {
    if (!el) return;
    opts = opts || {};
    if (typeof value === 'number' && value > 0) {
      el.textContent = `${label}: $${value.toFixed(2)}`;
      el.style.background = opts.activeBg || 'rgba(255,138,138,0.18)';
      el.style.color = opts.activeFg || '#ff8a8a';
      el.style.borderColor = opts.activeFg || '#ff8a8a';
    } else {
      el.textContent = `${label}: ${opts.offText || 'off'}`;
      el.style.background = 'var(--bg3)';
      el.style.color = 'var(--text3)';
      el.style.borderColor = 'var(--border)';
    }
  }

  // Tier downgrade — amber/yellow accent (the first remediation rung).
  _setChip(
    document.getElementById(`cm-lever-tier-${botId}`),
    'tier',
    _resolveEffective(setup.tier_downgrade_usd, setup.pod_tier_downgrade_usd),
    { activeBg: 'rgba(255,200,100,0.18)', activeFg: '#ffc864' },
  );

  // L1 breaker — red accent. effective_daily_hard_usd already handles the
  // override-or-derived path on the server, so the chip can use it directly.
  const dailyEl = document.getElementById(`cm-lever-dailycap-${botId}`);
  if (dailyEl) {
    const explicit = setup.daily_hard_usd;
    const effective = setup.effective_daily_hard_usd;
    const cap = (explicit === 0) ? null  // matrix "Off"
      : (typeof explicit === 'number' && explicit > 0) ? explicit
      : (typeof effective === 'number' && effective > 0) ? effective
      : null;
    _setChip(dailyEl, 'L1', cap);
  }

  // L2 breaker — deep red accent (gateway-stop; the strongest remediation).
  _setChip(
    document.getElementById(`cm-lever-l2-${botId}`),
    'L2',
    _resolveEffective(setup.l2_breaker_usd, setup.pod_l2_breaker_usd),
    { activeBg: 'rgba(220,38,38,0.25)', activeFg: '#dc2626' },
  );

  // Session cap — blue accent (sentinel, distinct from remediation tiers).
  _setChip(
    document.getElementById(`cm-lever-budget-${botId}`),
    'session',
    _resolveEffective(setup.session_cost_cap_usd, setup.pod_session_cost_cap_usd),
    { activeBg: 'rgba(127,200,255,0.18)', activeFg: '#7fc8ff', offText: 'none' },
  );
}

// ── Cost-levers modal: REMOVED (Phase 3 of cost-cap normalization) ──────
// All cost knobs (caps + Context & Session settings + cache TTL) now live
// on the Cost Optimization page (per-bot tab). The Settings page Cost &
// caps card is just a deep link there. Bot-tile chips and Configure
// buttons jump straight to the canonical editor via _cmGoToCostCaps,
// which is a thin alias over _jumpToCostOptForBot (PR C, used by the
// Settings deep link too) so the two deep-link surfaces share one path.

function _cmGoToCostCaps(botId) {
  if (typeof _jumpToCostOptForBot === 'function') {
    _jumpToCostOptForBot(botId);
    return;
  }
  // Fallback if the helper failed to load — at least navigate to the page.
  const navEl = document.querySelector('[data-page="cost-measures"]');
  if (navEl && typeof nav === 'function') nav(navEl);
}

function _cmRenderBotTabs() {
  const tabsEl = document.getElementById('cm-bot-tabs');
  if (!tabsEl) return;
  const network = _statusData || {};
  const bots = orderedBotIds(network.bots);
  if (!bots.length) return;
  // POD tab — pod-wide overview surface. data-bot="" is the sentinel for
  // "no bot selected → pod view". Count badge sums every open cost proposal
  // across all bots so the operator sees the aggregate at a glance.
  const podCount = Object.values(_cmProposalsByBot).reduce(
    (n, list) => n + ((list && list.length) || 0), 0);
  const podBadge = podCount > 0
    ? `<span class="badge" title="${podCount} open cost proposal${podCount === 1 ? '' : 's'} pod-wide" style="background:rgba(127,200,255,0.18);color:#7fc8ff;font-size:0.65rem;padding:0 5px;margin-left:5px;border-radius:9px">${podCount}</span>`
    : '';
  const podTab = `<div class="subtab${_cmBot === '' ? ' active' : ''}" data-bot="" onclick="_cmSelectBot('')">POD${podBadge}</div>`;
  const botTabs = bots.map(b => {
    const count = (_cmProposalsByBot[b] || []).length;
    const badge = count > 0
      ? `<span class="badge" title="${count} open cost proposal${count === 1 ? '' : 's'}" style="background:rgba(127,200,255,0.18);color:#7fc8ff;font-size:0.65rem;padding:0 5px;margin-left:5px;border-radius:9px">${count}</span>`
      : '';
    return `<div class="subtab${b === _cmBot ? ' active' : ''}" data-bot="${escHtml(b)}" onclick="_cmSelectBot('${escHtml(b)}')">${escHtml(botLabel(b))}${badge}</div>`;
  }).join('');
  tabsEl.innerHTML = podTab + botTabs;
}

// ── Cost & Caps matrix (per-bot tab + POD tab) ─────────────────────────────
//
// Single matrix-rendering function with two mounts: per-bot on a bot tab,
// pod-default on the POD tab. Same chip-based tristate pattern as the
// Context & Session Settings matrix:
//
//   per-bot scope:  [Default] [Off] [Custom $___]   (3 chips per row)
//   pod-default:    [Off] [Custom $___]              (2 chips — no inherit)
//
// "Default" writes null to the field (clears the per-bot override; bot
// inherits the pod default). "Off" writes 0 (the _pos resolver in
// spend_alert.py treats <=0 as no enforcement, so the field is stored
// but evaluates as off). "Custom" writes the operator's number.
//
// Reads + writes go through /api/arbiter/bot-setup/<bot> (per-bot) and
// /api/arbiter/pod-defaults (pod-default). Spec:
// docs/spec-cost-caps-2026-06-05.md §"UI".

const _CM_CAP_FIELDS = [
  // ── ALERTS · notify-only ─────────────────────────────────────────
  // Labels are "<window> warn" across the board so the operator scans
  // the section vertically without re-parsing each row — monthly,
  // weekly, daily, pod-total weekly all share the "warn" suffix.
  { group: 'Alerts · notify only',
    key: 'monthly_cap_usd', label: 'Monthly warn',
    desc: 'Notifies the operator when month-to-date spend crosses this. Alert-only — no enforcement action.' },
  { group: 'Alerts · notify only',
    key: 'weekly_warn_usd', label: 'Weekly warn',
    desc: 'Notifies the operator when rolling 7-day spend crosses this. Deduped once per ISO week.' },
  { group: 'Alerts · notify only',
    key: 'daily_warn_usd', label: 'Daily warn',
    desc: "Notifies the operator when today's spend crosses this. Deduped once per day (pod local TZ)." },
  { group: 'Alerts · notify only', podOnly: true,
    key: 'pod_weekly_warn_usd', label: 'Pod-total weekly warn',
    desc: "Notifies the operator when the SUM of every bot's rolling 7-day spend crosses this. Pod aggregate only — has no per-bot equivalent." },

  // ── REMEDIATION · graduated enforcement ──────────────────────────
  // Labels read as the level of intervention paired with the window
  // so the operator can map each row to "what this changes" at a
  // glance. All three remediation thresholds are evaluated against
  // today's spend (pod local TZ).
  { group: 'Remediation · graduated enforcement',
    key: 'tier_downgrade_usd', label: 'Tier downgrade (daily)',
    desc: "Auto-switches the bot's primary model to tier 3 for the rest of the day when crossed. Auto-reverts at midnight (pod local TZ)." },
  { group: 'Remediation · graduated enforcement',
    key: 'daily_hard_usd', label: 'L1 circuit breaker (daily) — stop auto sessions',
    desc: 'Trips the L1 cost breaker — heartbeat & background sessions pause; user chat keeps working. Auto-resets after 24h.' },
  { group: 'Remediation · graduated enforcement',
    key: 'l2_breaker_usd', label: 'L2 circuit breaker (daily) — shut down gateway',
    desc: "Stops the bot's gateway entirely — no chat, no background. Manual reset required; no auto-revert." },

  // ── SENTINEL · in-flight session ceiling ─────────────────────────
  { group: 'Sentinel · in-flight session ceiling',
    key: 'session_cost_cap_usd', label: 'Per-session cap',
    desc: 'Rejects the next turn in any session that crosses this. Catches runaway conversations in flight.' },
];

// Per-bot caps payload returned by /api/arbiter/bot-setup/<bot>. Carries
// both explicit per-bot overrides (null = inherit) and the pod defaults
// the bot is inheriting, so the matrix can distinguish "inherited" from
// "explicit" and show the pod-default column accurately.
let _cmCurrentBotCaps = null;
// Pod-default caps payload returned by /api/arbiter/pod-defaults.
let _cmPodCaps = null;

async function _cmLoadBotCaps() {
  const el = document.getElementById('cm-bot-caps-body');
  const lbl = document.getElementById('cm-bot-caps-label');
  if (!el) return;
  if (!_cmBot) { el.innerHTML = '<div class="empty">Select a bot above.</div>'; return; }
  if (lbl) lbl.textContent = `— ${_cmBot}`;
  el.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';
  try {
    // Load the per-bot overrides AND the pod defaults together. The matrix's
    // "Default (X)" chips resolve inherited values from _cmPodCaps (see
    // _cmPodDefaultFor), so it must be populated before render — otherwise a
    // bot tab opened without first visiting POD would show "Default (off)"
    // for every row that actually inherits a pod value.
    const [botCaps] = await Promise.all([
      api('GET', `/api/arbiter/bot-setup/${encodeURIComponent(_cmBot)}`),
      _cmFetchPodCaps(),
    ]);
    _cmCurrentBotCaps = botCaps;
    el.innerHTML = _cmRenderCapsMatrix('per-bot');
  } catch (e) {
    el.innerHTML = `<div class="empty">Could not load cost &amp; caps: ${escHtml(String(e))}</div>`;
  }
}

async function _cmRefreshBotCaps() { await _cmLoadBotCaps(); }

// Fetch the pod-default caps into module state (no render). Shared by the POD
// matrix loader and the per-bot loader so both read identical inherited
// values. Kept separate from _cmLoadPodCaps so the per-bot tab can refresh
// pod defaults without touching the hidden POD body.
async function _cmFetchPodCaps() {
  _cmPodCaps = await api('GET', '/api/arbiter/pod-defaults');
  return _cmPodCaps;
}

async function _cmLoadPodCaps() {
  const el = document.getElementById('cm-pod-caps-body');
  if (!el) return;
  el.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';
  try {
    await _cmFetchPodCaps();
    el.innerHTML = _cmRenderCapsMatrix('pod-default');
  } catch (e) {
    el.innerHTML = `<div class="empty">Could not load pod defaults: ${escHtml(String(e))}</div>`;
  }
}

async function _cmRefreshPodCaps() { await _cmLoadPodCaps(); }

// ── Pre-flight router card (POD view only) ──────────────────────────────────
//
// Renders into #cm-preflight-body. Calls /api/preflight/health and shows a
// per-bot table: total decisions, rates (with threshold color-coding), top
// firing reason, haiku cost projection.
//
// Pinned by tests in packages/admin/tests/test_routes_preflight.py — the
// API shape is stable, this renderer is the only consumer for now.
async function _cmLoadPreflight() {
  const el = document.getElementById('cm-preflight-body');
  if (!el) return;
  el.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';
  try {
    const data = await api('GET', '/api/preflight/health');
    el.innerHTML = _cmRenderPreflight(data);
  } catch (e) {
    el.innerHTML = `<div class="empty">Could not load pre-flight stats: ${escHtml(String(e))}</div>`;
  }
}

async function _cmRefreshPreflight() { await _cmLoadPreflight(); }

// Color-code a rate value vs its alert threshold. Returns a CSS color
// the body of the per-bot row can paint with. The thresholds come from
// the API response so a future threshold change updates the UI without
// a frontend edit.
function _cmPreflightRateColor(pct, alertPct) {
  if (pct == null) return 'var(--text2)';
  if (pct >= alertPct) return 'var(--red)';
  if (pct >= alertPct * 0.66) return 'var(--yellow)';
  return 'var(--green)';
}

function _cmRenderPreflight(data) {
  const byBot = data?.by_bot || {};
  const thresholds = data?.thresholds || {};
  const totals = data?.totals || {};
  const minDecisions = thresholds.min_decisions ?? 30;

  const botIds = Object.keys(byBot).sort();
  if (!botIds.length) {
    return `
      <div class="subtle" style="font-size:0.82rem">
        No pre-flight spans yet — the router runs on user_turn triggers only,
        and either no user-turns have happened in the window, or the
        deployment hasn't seen the post-#2334 plugin yet. Verify on the mini
        by checking <code>cascade.preflight.layer</code> attribute on recent
        spans.
      </div>`;
  }

  // Pod-wide rollup at the top
  const podRollup = `
    <div style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:12px;font-size:0.85rem">
      <div><strong>${(totals.preflight_ran ?? 0).toLocaleString()}</strong> turns routed
        <span class="subtle" style="font-size:0.78rem">(of ${(totals.spans_seen ?? 0).toLocaleString()} total)</span></div>
      <div><strong>${(totals.haiku_calls ?? 0).toLocaleString()}</strong> haiku calls
        <span class="subtle" style="font-size:0.78rem">(~$${(totals.haiku_estimated_cost_usd ?? 0).toFixed(4)})</span></div>
      <div class="subtle" style="font-size:0.78rem">window: ${data.window_days ?? 7}d</div>
    </div>`;

  // Per-bot table
  const rows = botIds.map(botId => {
    const b = byBot[botId];
    const decisions = b.decisions ?? 0;
    const lowSample = decisions < minDecisions;
    const rates = b.rates || {};

    // Top firing reason — across all categories, highest count
    const reasons = b.by_reason || {};
    let topReason = null;
    let topCount = 0;
    for (const [r, counts] of Object.entries(reasons)) {
      const n = counts.count || 0;
      if (n > topCount) { topCount = n; topReason = r; }
    }

    const formatRate = (rateKey, thresholdKey) => {
      const pct = (rates[rateKey] ?? 0) * 100;
      const alertPct = thresholds[thresholdKey] ?? 100;
      if (lowSample) {
        return `<span class="subtle" title="sample too small">${pct.toFixed(1)}%</span>`;
      }
      const color = _cmPreflightRateColor(pct, alertPct);
      return `<span style="color:${color};font-weight:600">${pct.toFixed(1)}%</span>`;
    };

    const haikuCost = b.haiku_estimated_cost_usd ?? 0;
    const haikuP50 = b.haiku_latency_ms_p50;
    const haikuLatency = haikuP50 != null
      ? `<span class="subtle" style="font-size:0.74rem">${haikuP50.toFixed(0)}ms p50</span>`
      : '';

    return `
      <tr>
        <td style="padding:5px 8px;font-weight:600">${escHtml(botLabel(botId))}</td>
        <td style="padding:5px 8px;text-align:right;font-variant-numeric:tabular-nums">
          ${decisions}
          ${lowSample ? '<span class="subtle" style="font-size:0.7rem" title="below ' + minDecisions + ' — sample too small for rates"> (low)</span>' : ''}
        </td>
        <td style="padding:5px 8px;text-align:right;font-variant-numeric:tabular-nums">
          ${formatRate('agreement_rate', 'over_escalation_pct')}
        </td>
        <td style="padding:5px 8px;text-align:right;font-variant-numeric:tabular-nums">
          ${formatRate('over_escalation_rate', 'over_escalation_pct')}
        </td>
        <td style="padding:5px 8px;text-align:right;font-variant-numeric:tabular-nums">
          ${formatRate('under_escalation_rate', 'under_escalation_pct')}
        </td>
        <td style="padding:5px 8px;text-align:right;font-variant-numeric:tabular-nums">
          ${formatRate('cascade_corrected_rate', 'cascade_corrected_pct')}
        </td>
        <td style="padding:5px 8px;text-align:right;font-variant-numeric:tabular-nums">
          ${b.haiku_call_count ?? 0}
          ${haikuLatency ? ' · ' + haikuLatency : ''}
          ${haikuCost > 0 ? `<span class="subtle" style="font-size:0.74rem"> · $${haikuCost.toFixed(4)}</span>` : ''}
        </td>
        <td style="padding:5px 8px;font-size:0.74rem;color:var(--text2);max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escHtml(topReason || '')}">
          ${escHtml(topReason || '—')}
          ${topCount > 0 ? ` <span class="subtle">×${topCount}</span>` : ''}
        </td>
      </tr>`;
  }).join('');

  return `
    ${podRollup}
    <div style="overflow-x:auto">
      <table style="width:100%;border-collapse:collapse;font-size:0.82rem">
        <thead>
          <tr style="border-bottom:1px solid var(--border);color:var(--text2);font-size:0.74rem">
            <th style="text-align:left;padding:4px 8px;font-weight:500">Bot</th>
            <th style="text-align:right;padding:4px 8px;font-weight:500">Decisions</th>
            <th style="text-align:right;padding:4px 8px;font-weight:500" title="Decisions where pre-flight picked the right tier">Agree</th>
            <th style="text-align:right;padding:4px 8px;font-weight:500" title="Tier1 picked, turn was trivially handled — wasted Opus pricing">Over-esc</th>
            <th style="text-align:right;padding:4px 8px;font-weight:500" title="Tier3 picked, turn struggled — user did poorly on Haiku">Under-esc</th>
            <th style="text-align:right;padding:4px 8px;font-weight:500" title="Cascade escalated the next turn — pre-flight under-shot">Casc-fix</th>
            <th style="text-align:right;padding:4px 8px;font-weight:500">Haiku</th>
            <th style="text-align:left;padding:4px 8px;font-weight:500" title="Top firing pre-flight reason for this bot">Top reason</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

// The pod-default a matrix row inherits, read from the canonical
// /api/arbiter/pod-defaults payload (_cmPodCaps). This is the SINGLE source
// of truth for "what a bot inherits when it has no override": both the POD
// matrix (where these values ARE the row value) and every per-bot
// "Default (X)" chip resolve through here, so the two surfaces can never
// disagree. The pod-defaults endpoint uses per_bot_* names for the daily
// warn/hard inheritance values; map them to the matrix's canonical keys.
function _cmPodDefaultFor(key) {
  const p = _cmPodCaps || {};
  const podMap = {
    monthly_cap_usd:      p.monthly_cap_usd,
    weekly_warn_usd:      p.weekly_warn_usd,
    daily_warn_usd:       p.per_bot_daily_warn_usd,
    pod_weekly_warn_usd:  p.pod_weekly_warn_usd,
    tier_downgrade_usd:   p.tier_downgrade_usd,
    daily_hard_usd:       p.per_bot_daily_hard_usd,
    l2_breaker_usd:       p.l2_breaker_usd,
    session_cost_cap_usd: p.session_cost_cap_usd,
  };
  return podMap[key] ?? null;
}

// A row's raw per-bot override + the pod default it would inherit.
//   - per-bot OVERRIDE comes from /api/arbiter/bot-setup (b[key]); null means
//     "no override — inherit the pod default."
//   - the pod DEFAULT always comes from _cmPodDefaultFor (pod-defaults
//     endpoint), never the bot-setup `pod_*` mirror.
// The old map read pod defaults from a MIX of the bot-setup `pod_*` mirror
// (monthly / daily_warn / daily_hard) and _cmPodCaps (the opt-in tiers),
// which is why the per-bot "Default (X)" chips drifted from the POD tab: the
// bot-setup-sourced rows showed "off" whenever that endpoint was unavailable,
// and the _cmPodCaps-sourced rows showed "off" until the POD tab had been
// visited to populate it. Reading every row from one endpoint removes both.
function _cmCapsValueFor(scope, key) {
  if (scope === 'pod-default') {
    // At pod scope the inherited value IS the row's value.
    return { override: _cmPodDefaultFor(key), podDefault: null };
  }
  // per-bot scope
  const b = _cmCurrentBotCaps || {};
  const override = b[key] ?? null;
  return { override, podDefault: _cmPodDefaultFor(key) };
}

// Classify a row's state for active-chip highlighting:
//   "default" → override is null (inheriting); only valid for per-bot scope
//   "off"     → override is 0 (explicit no-enforcement)
//   "custom"  → override is a positive number
function _cmCapsStateFor(scope, key) {
  const { override } = _cmCapsValueFor(scope, key);
  if (override === null || override === undefined) {
    return scope === 'per-bot' ? 'default' : 'off';
  }
  const n = Number(override);
  if (!Number.isFinite(n) || n <= 0) return 'off';
  return 'custom';
}

function _cmFormatUsd(n) {
  if (n == null) return '—';
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  return v >= 100
    ? `$${Math.round(v)}`
    : `$${v.toFixed(v < 10 ? 2 : 0)}`;
}

function _cmRenderCapsMatrix(scope) {
  const rows = _CM_CAP_FIELDS.filter(f => {
    if (scope !== 'pod-default' && f.podOnly) return false;
    return true;
  });

  const isPerBot = scope === 'per-bot';
  let lastGroup = null;
  // Two columns: Setting + Value. The previous "Pod default" column was
  // redundant — the per-bot Default chip already shows the pod value
  // inline ("Default ($5)"), so a third column duplicated the info and
  // ate horizontal room the row descriptions need when expanded.
  const colCount = 2;
  // Global header label flips based on whether ANY visible row is
  // currently expanded. "Visible" excludes pod-only rows when scope is
  // per-bot, so the label matches what the operator actually sees.
  const visibleKeys = rows.map(f => f.key);
  const anyExpanded = visibleKeys.some(k => _cmExpandedCapsRows.has(k));

  const renderRow = (field) => {
    let groupRow = '';
    if (field.group !== lastGroup) {
      groupRow = `<tr class="cm-caps-group-row"><td colspan="${colCount}" class="cm-caps-group-label">${escHtml(field.group)}</td></tr>`;
      lastGroup = field.group;
    }
    const { override, podDefault } = _cmCapsValueFor(scope, field.key);
    const state = _cmCapsStateFor(scope, field.key);
    const customN = (state === 'custom') ? Number(override) : null;
    const customStr = (customN != null && Number.isFinite(customN)) ? String(customN) : '';

    // Chips
    const chipBtn = (chipState, label) => {
      const active = state === chipState;
      const cls = active ? 'cm-caps-chip cm-caps-chip-active' : 'cm-caps-chip';
      return `<button type="button" class="${cls}"
                onclick="_cmCapsChipClick('${escHtml(scope)}','${escHtml(field.key)}','${chipState}')">${escHtml(label)}</button>`;
    };

    const defaultLabel = isPerBot
      ? (podDefault == null ? 'Default (off)' : `Default (${_cmFormatUsd(podDefault)})`)
      : null;

    const chips = [
      isPerBot ? chipBtn('default', defaultLabel) : '',
      chipBtn('off', 'Off'),
      // Custom chip with inline number input. When active the input has
      // the current value; when inactive it sits at placeholder. Pressing
      // Enter or blurring commits the write.
      `<span class="cm-caps-chip-custom ${state === 'custom' ? 'cm-caps-chip-active' : ''}">
        <span class="cm-caps-chip-custom-prefix">Custom</span>
        <span class="cm-caps-chip-custom-dollar">$</span>
        <input type="number" min="0.01" step="0.5" placeholder="—" value="${escHtml(customStr)}"
               data-caps-scope="${escHtml(scope)}" data-caps-field="${escHtml(field.key)}"
               onkeydown="if(event.key==='Enter'){event.target.blur();}"
               onblur="_cmCapsCustomCommit(this)"
               class="cm-caps-chip-custom-input">
      </span>`,
    ].filter(Boolean).join('');

    const isExpanded = _cmExpandedCapsRows.has(field.key);
    const descHtml = isExpanded
      ? `<div class="cm-caps-setting-desc">${escHtml(field.desc)}</div>`
      : '';
    // Per-row chevron — inline next to the label. The whole label row is
    // a clickable button so the operator doesn't have to hit the tiny
    // glyph precisely.
    const labelRow = `<button type="button" class="cm-caps-setting-label-row"
      onclick="_cmToggleCapsRow('${escHtml(scope)}','${escHtml(field.key)}')"
      title="${isExpanded ? 'Hide' : 'Show'} description">
      <span class="cm-caps-setting-chevron expand-icon${isExpanded ? ' is-open' : ''}" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
      <span class="cm-caps-setting-label">${escHtml(field.label)}</span>
    </button>`;
    const settingCell = `<td class="cm-caps-setting-cell">
      ${labelRow}
      ${descHtml}
    </td>`;
    const valueCell = `<td class="cm-caps-value-cell">${chips}</td>`;

    return groupRow + `<tr>${settingCell}${valueCell}</tr>`;
  };

  const headers = `<tr>
        <th class="cm-caps-th-setting">Setting</th>
        <th class="cm-caps-th-value">Value</th>
       </tr>`;

  return `
    <style>
      .cm-caps-matrix { width:100%; border-collapse:separate; border-spacing:0; }
      .cm-caps-matrix th { padding:8px 10px; font-size:0.72rem; color:var(--text3); text-transform:uppercase; letter-spacing:0.05em; text-align:left; border-bottom:1px solid var(--border); }
      .cm-caps-th-value { text-align:left; }
      .cm-caps-setting-cell { padding:9px 12px 9px 8px; vertical-align:top; border-bottom:1px solid rgba(255,255,255,0.04); width:46%; }
      .cm-caps-setting-label { font-weight:600; font-size:0.85rem; color:var(--text); }
      .cm-caps-setting-desc { font-size:0.72rem; color:var(--text3); margin-top:4px; line-height:1.45; max-width:520px; padding-left:14px; }
      .cm-caps-setting-label-row { display:flex; align-items:baseline; gap:6px; background:transparent; border:none; padding:0; margin:0; cursor:pointer; text-align:left; color:inherit; font:inherit; width:100%; }
      .cm-caps-setting-label-row:hover .cm-caps-setting-chevron { color:var(--accent); }
      .cm-caps-setting-chevron { font-size:0.7rem; color:var(--text3); width:10px; flex-shrink:0; transition:color 0.1s; }
      .cm-caps-value-cell { padding:9px 10px; vertical-align:middle; border-bottom:1px solid rgba(255,255,255,0.04); }
      .cm-caps-group-row td { background:transparent; }
      .cm-caps-group-label { padding:12px 8px 4px; font-size:0.7rem; color:var(--text3); text-transform:uppercase; letter-spacing:0.05em; font-weight:600; border-top:1px solid var(--border); }
      .cm-caps-chip { display:inline-block; padding:4px 11px; margin:0 4px 4px 0; border-radius:14px; border:1px solid var(--border); background:var(--bg2); color:var(--text); font-size:0.8rem; cursor:pointer; transition:background 0.1s,border-color 0.1s; }
      .cm-caps-chip:hover { background:var(--bg3); }
      .cm-caps-chip-active { background:rgba(127,200,255,0.18); border-color:rgba(127,200,255,0.55); color:#7fc8ff; font-weight:600; }
      .cm-caps-chip-custom { display:inline-flex; align-items:center; padding:3px 9px 3px 11px; margin:0 4px 4px 0; border-radius:14px; border:1px solid var(--border); background:var(--bg2); font-size:0.8rem; }
      .cm-caps-chip-custom-prefix { color:var(--text); margin-right:8px; }
      .cm-caps-chip-custom-dollar { color:var(--text3); margin-right:2px; }
      .cm-caps-chip-custom-input { width:72px; background:transparent; border:none; color:var(--text); font-size:0.8rem; outline:none; padding:1px 0; -moz-appearance:textfield; }
      .cm-caps-chip-custom-input::-webkit-outer-spin-button,
      .cm-caps-chip-custom-input::-webkit-inner-spin-button { -webkit-appearance:none; margin:0; }
      .cm-caps-chip-custom-input::placeholder { color:var(--text3); }
      .cm-caps-chip-active.cm-caps-chip-custom { background:rgba(127,200,255,0.18); border-color:rgba(127,200,255,0.55); }
      .cm-caps-chip-active .cm-caps-chip-custom-prefix { color:#7fc8ff; font-weight:600; }
    </style>
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;gap:12px;flex-wrap:wrap">
      <div style="font-size:0.78rem;color:var(--text3);flex:1;min-width:200px">${isPerBot
        ? 'Click <em>Default</em> to inherit the pod-wide value, <em>Off</em> to disable the cap, or type a number in <em>Custom</em>. Each row writes immediately.'
        : 'These values apply to every bot unless that bot sets an explicit override. Click <em>Off</em> to disable, or type a number in <em>Custom</em>.'}</div>
      <button class="btn btn-ghost btn-sm" style="white-space:nowrap;font-size:0.74rem" onclick="_cmToggleAllCapsRows('${escHtml(scope)}')">${anyExpanded ? 'Collapse all' : 'Expand all'}</button>
    </div>
    <div style="overflow-x:auto">
      <table class="cm-caps-matrix">
        <thead>${headers}</thead>
        <tbody>${rows.map(renderRow).join('')}</tbody>
      </table>
    </div>
    <div id="cm-caps-${isPerBot ? 'bot' : 'pod'}-status" style="margin-top:10px;font-size:0.78rem;min-height:1.1em"></div>`;
}

// Encode a chip click. `chipState` ∈ {"default","off","custom"}. Default
// writes null (clears override); Off writes 0; Custom is a no-op here —
// the inline input handles its own commit on blur.
async function _cmCapsChipClick(scope, fieldKey, chipState) {
  if (chipState === 'custom') {
    // Focus the input so the operator can type. No write here.
    const sel = `input[data-caps-scope="${scope}"][data-caps-field="${fieldKey}"]`;
    const input = document.querySelector(sel);
    if (input) input.focus();
    return;
  }
  const value = chipState === 'off' ? 0 : null;
  await _cmCapsWrite(scope, fieldKey, value);
}

async function _cmCapsCustomCommit(input) {
  const scope = input.dataset.capsScope;
  const field = input.dataset.capsField;
  const raw = input.value.trim();
  if (raw === '') {
    // Empty input = no commit (user blurred without entering anything).
    // Reload so the chip state reflects whatever was committed last.
    if (scope === 'per-bot') await _cmLoadBotCaps(); else await _cmLoadPodCaps();
    return;
  }
  const v = parseFloat(raw);
  if (!Number.isFinite(v) || v <= 0) {
    _cmCapsSetStatus(scope, 'Custom value must be a positive number.', 'error');
    return;
  }
  await _cmCapsWrite(scope, field, v);
}

async function _cmCapsWrite(scope, fieldKey, value) {
  const url = scope === 'per-bot'
    ? `/api/arbiter/bot-setup/${encodeURIComponent(_cmBot)}`
    : '/api/arbiter/pod-defaults';
  _cmCapsSetStatus(scope, 'Saving…', '');
  try {
    // api() resolves (does not throw) on an HTTP error — it returns the
    // server's {error} payload. A 500 here used to slip through as "Saved."
    // while nothing persisted, which is exactly how a backend outage looked
    // like an inert Custom field. Inspect the payload and surface the failure.
    const res = await api('POST', url, { [fieldKey]: value });
    if (res && res.error) {
      _cmCapsSetStatus(scope, `Failed: ${String(res.error)}`, 'error');
      return;
    }
    _cmCapsSetStatus(scope, 'Saved.', 'success');
    if (scope === 'per-bot') await _cmLoadBotCaps(); else await _cmLoadPodCaps();
  } catch (e) {
    _cmCapsSetStatus(scope, `Failed: ${String(e && e.message || e)}`, 'error');
  }
}

function _cmCapsSetStatus(scope, text, kind) {
  const el = document.getElementById(`cm-caps-${scope === 'per-bot' ? 'bot' : 'pod'}-status`);
  if (!el) return;
  const color = kind === 'success' ? 'var(--green)'
              : kind === 'error'   ? 'var(--red)'
              : 'var(--text3)';
  el.innerHTML = `<span style="color:${color}">${escHtml(text)}</span>`;
}

// Cost-domain health chips at top of the Cost Measures page. Surfaces
// pod-wide operational signals: spend caps currently being enforced,
// runaway sessions detected in the last 24h. Click any chip to scroll
// to the section that explains it. Mirrors the bot-tile chip pattern
// so the visual vocabulary is consistent across pages.
async function _cmLoadChips() {
  const el = document.getElementById('cm-chips');
  if (!el) return;
  const chips = [];
  // Caps — active enforcement, plus warning state and recent-trip history.
  try {
    const d = await api('GET', '/api/spend-caps');
    const active = d?.active_enforcement || {};
    const activeBots = Object.keys(active);
    if (activeBots.length > 0) {
      chips.push({
        severity: 'critical',
        label: `${activeBots.length} cap active`,
        detail: `enforcement on ${activeBots.slice(0, 3).join(', ')}${activeBots.length > 3 ? '…' : ''}`,
        scroll: '#cm-bot-caps-card',
      });
    }
    // cap_warning — bots at >=80% of daily cap, not yet enforced.
    const warns = d?.cap_warnings || {};
    const warnBots = Object.keys(warns);
    if (warnBots.length > 0) {
      const detail = warnBots.slice(0, 3).map(b =>
        `${b} ${Math.round((warns[b]?.pct || 0) * 100)}%`
      ).join(', ');
      chips.push({
        severity: 'warn',
        label: `${warnBots.length} cap warning${warnBots.length === 1 ? '' : 's'}`,
        detail: `${detail}${warnBots.length > 3 ? '…' : ''} of daily cap`,
        scroll: '#cm-bot-caps-card',
      });
    }
    // recent_enforcement_history — bots that tripped a cap in the last 7d
    // but aren't currently enforced. Surfaces recurring offenders.
    const recent = d?.recent_enforcement_history || {};
    const recentBots = Object.keys(recent);
    if (recentBots.length > 0) {
      const totalTrips = Object.values(recent).reduce((a, b) => a + b, 0);
      chips.push({
        severity: 'warn',
        label: `${totalTrips} recent cap trip${totalTrips === 1 ? '' : 's'}`,
        detail: `${recentBots.slice(0, 3).join(', ')}${recentBots.length > 3 ? '…' : ''} in last 7d`,
        scroll: '#cm-bot-caps-card',
      });
    }
    // active_breakers — bots whose L1 cost breaker is currently up.
    // Distinct from active_enforcement: a breaker can be tripped by the
    // bloat detector or a manual trip without a spend-cap configured.
    // Without surfacing it here, the "✓ Healthy" fallback below would
    // fire even when a bot in the tile row visibly shows the "breaker
    // tripped" chip — the bug behind the empty summary band reported
    // on the test pod.
    const breakers = d?.active_breakers || {};
    const breakerBots = Object.keys(breakers);
    if (breakerBots.length > 0) {
      chips.push({
        severity: 'critical',
        label: `${breakerBots.length} breaker${breakerBots.length === 1 ? '' : 's'} tripped`,
        detail: `auto turns suspended on ${breakerBots.slice(0, 3).join(', ')}${breakerBots.length > 3 ? '…' : ''}`,
        scroll: '#cm-bot-tiles',
      });
    }
  } catch { /* ignore — chip simply won't fire */ }
  // Runaway — pod-wide last 24h.
  try {
    const d = await api('GET', '/api/analytics/sessions/runaway?days=1');
    let totalFlagged = 0;
    const offending = [];
    for (const [bot, body] of Object.entries(d || {})) {
      const n = (body?.flagged_sessions || []).length;
      if (n > 0) { totalFlagged += n; offending.push(bot); }
    }
    if (totalFlagged > 0) {
      chips.push({
        severity: 'critical',
        label: `${totalFlagged} runaway session${totalFlagged === 1 ? '' : 's'}`,
        detail: `flagged on ${offending.slice(0, 3).join(', ')}${offending.length > 3 ? '…' : ''} in last 24h`,
        scroll: '#cm-runaway-body',
      });
    }
  } catch { /* ignore */ }
  if (chips.length === 0) {
    el.innerHTML = `<span class="pod-chip pod-chip-ok">✓ Healthy</span>`;
    return;
  }
  el.innerHTML = chips.map(c => {
    const cls = `pod-chip pod-chip-${c.severity} pod-chip-clickable`;
    const title = escHtml(c.detail || '');
    const label = escHtml(c.label);
    const sel = escHtml(c.scroll || '');
    return `<span class="${cls}" title="${title}" onclick="chipScroll('${sel}')">${label}</span>`;
  }).join('');
}

function _cmSelectBot(botId) {
  _cmBot = botId || '';
  // Match by data-bot — display names diverge from bot ids after a rename.
  // data-bot="" is the POD tab.
  document.querySelectorAll('#cm-bot-tabs .subtab').forEach(t => {
    t.classList.toggle('active', (t.dataset.bot || '') === _cmBot);
  });
  // Tile-driven selection: the tile row above also reflects the
  // currently-selected bot so the UI tells one story. On the POD tab
  // (_cmBot === '') no tile is active — that's correct, the whole row
  // is the overview.
  document.querySelectorAll('#cm-bot-tiles .cm-bot-tile').forEach(t => {
    t.classList.toggle('active', (t.dataset.bot || '') === _cmBot);
  });
  // Toggle the two top-level content containers. POD view shows the
  // pod-wide budget + pod-wide open cost proposals; bot view shows the
  // per-bot sections (score, settings, profiles, caps, runaway, audit,
  // spike explorer).
  const podView = document.getElementById('cm-pod-view');
  const botView = document.getElementById('cm-bot-view');
  if (podView) podView.style.display = _cmBot === '' ? '' : 'none';
  if (botView) botView.style.display = _cmBot === '' ? 'none' : '';
  _cmRenderBotProposalsStrip();
  loadCostMeasures();
}

// ── Section 1: Cost Efficiency Score ─────────────────────────────────────────

async function _cmLoadScore() {
  const el = document.getElementById('cm-score-body');
  if (!el) return;
  if (!_cmBot) { el.innerHTML = '<div class="empty">Select a bot above.</div>'; return; }
  el.innerHTML = '<div class="loading"><span class="spinner"></span> Computing score…</div>';
  try {
    const d = await api('GET', `/api/bot/cost-score?bot=${encodeURIComponent(_cmBot)}&days=7`);
    el.innerHTML = _cmRenderScore(d);
  } catch(e) {
    el.innerHTML = `<div class="empty">Score unavailable: ${escHtml(String(e))}</div>`;
  }
}

// ── Section 1b: App Bootstrap Footprint ──────────────────────────────────────
//
// Surfaces how many bytes (and estimated tokens / $) each installed app
// contributes to every-turn system-prompt injection. Info-only during the
// calibration window — colors match the per-app verdict the backend
// returns ('ok' | 'info' | 'warn'). The bot-level verdict colors the
// header pill so the operator's eye lands there first.

async function _cmLoadAppFootprint() {
  const el = document.getElementById('cm-app-footprint-body');
  if (!el) return;
  if (!_cmBot) { el.innerHTML = '<div class="empty">Select a bot above.</div>'; return; }
  el.innerHTML = '<div class="loading"><span class="spinner"></span> Measuring…</div>';
  try {
    const d = await api('GET', `/api/bot/app-bootstrap-footprint?bot=${encodeURIComponent(_cmBot)}`);
    el.innerHTML = _cmRenderAppFootprint(d);
  } catch(e) {
    el.innerHTML = `<div class="empty">Footprint unavailable: ${escHtml(String(e))}</div>`;
  }
}

const _CM_FOOTPRINT_COLORS = {
  ok:    { fg: 'var(--green)',  bg: 'rgba(34,197,94,0.10)'  },
  info:  { fg: 'var(--text2)',  bg: 'rgba(127,127,127,0.10)' },
  warn:  { fg: 'var(--yellow)', bg: 'rgba(234,179,8,0.12)'   },
  alert: { fg: 'var(--red)',    bg: 'rgba(239,68,68,0.12)'   },
};

function _cmFmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return `${n} B`;
  return `${(n / 1024).toFixed(1)} KB`;
}

// Per-app transport-choice hint. Surfaces the same finding as
// app_audit_structural's app_cron_eligible_used_heartbeat /
// app_invocation_mode_not_subagent — but inline on the chip where the
// operator is already looking at byte counts, instead of as an
// info-severity Signal in the Alerts queue's "show info" view.
// principle-apps-minimize-bootstrap-cost.md is the authoritative source.
const _CM_TRANSPORT_HINT_TEXT = {
  could_be_cron: {
    label: 'could be cron',
    title: 'Declares heartbeat_evidence but no recursive_llm.purposes — could run as a cron with no per-turn bootstrap cost.',
  },
  could_be_subagent: {
    label: 'could be subagent',
    title: 'User-routed app with LLM intent + CLI but invocation_mode is not "subagent" — riding the bot main session inherits accumulated context for no benefit.',
  },
};

function _cmTransportHintBadge(a) {
  const hint = a && a.transport_hint;
  if (!hint || !_CM_TRANSPORT_HINT_TEXT[hint]) return '';
  const h = _CM_TRANSPORT_HINT_TEXT[hint];
  return ` <span class="badge badge-blue" title="${escHtml(h.title)}" style="margin-left:6px;vertical-align:middle">${escHtml(h.label)}</span>`;
}

function _cmRenderAppFootprint(d) {
  if (!d || d.error) {
    return `<div class="empty">No footprint data: ${escHtml(d?.error || 'unknown')}</div>`;
  }
  if (!d.app_count) {
    return `<div class="empty">No apps installed on this bot.</div>`;
  }
  const verdict = d.verdict || 'ok';
  const c = _CM_FOOTPRINT_COLORS[verdict] || _CM_FOOTPRINT_COLORS.ok;
  const tokens = d.total_tokens_estimated || 0;
  const usd = (d.estimated_cost_per_cache_miss_usd || 0).toFixed(4);
  const apps = d.apps || [];
  const thresholds = d.thresholds || {};

  const headline = `
    <div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap;padding:10px 12px;border-radius:6px;background:${c.bg};border:1px solid ${c.fg};margin-bottom:12px">
      <div>
        <div style="font-size:0.72rem;color:var(--text3);text-transform:uppercase;letter-spacing:0.04em">Per-turn injection</div>
        <div style="font-size:1.2rem;font-weight:700;color:${c.fg}">${_cmFmtBytes(d.total_bytes)}</div>
      </div>
      <div>
        <div style="font-size:0.72rem;color:var(--text3);text-transform:uppercase;letter-spacing:0.04em">Tokens (est.)</div>
        <div style="font-size:1rem">${tokens.toLocaleString()}</div>
      </div>
      <div>
        <div style="font-size:0.72rem;color:var(--text3);text-transform:uppercase;letter-spacing:0.04em">$/cache-miss turn</div>
        <div style="font-size:1rem">$${usd}</div>
      </div>
      <div style="flex:1"></div>
      <div style="font-size:0.72rem;color:var(--text3);max-width:280px;text-align:right;line-height:1.4">
        Warn at ${_cmFmtBytes(thresholds.per_bot_aggregate_warn_bytes)} · alert at ${_cmFmtBytes(thresholds.per_bot_aggregate_alert_bytes)}<br/>
        Estimated at ${escHtml(d.model_used_for_estimate || '')} input pricing
      </div>
    </div>
  `;

  function rowFor(a) {
    const ac = _CM_FOOTPRINT_COLORS[a.verdict || 'ok'] || _CM_FOOTPRINT_COLORS.ok;
    return `
      <tr>
        <td style="padding:6px 8px;color:${ac.fg}">●</td>
        <td style="padding:6px 8px"><b>${escHtml(a.display_name || a.id)}</b>${_cmTransportHintBadge(a)}<div style="font-size:0.7rem;color:var(--text3)">${escHtml(a.id)}</div></td>
        <td style="padding:6px 8px;text-align:right;font-variant-numeric:tabular-nums">${_cmFmtBytes(a.bot_guidance_bytes)}</td>
        <td style="padding:6px 8px;text-align:right;font-variant-numeric:tabular-nums">${_cmFmtBytes(a.installed_apps_entry_bytes)}</td>
        <td style="padding:6px 8px;text-align:right;font-variant-numeric:tabular-nums">${_cmFmtBytes(a.tool_defs_bytes)}</td>
        <td style="padding:6px 8px;text-align:right;font-variant-numeric:tabular-nums;font-weight:600">${_cmFmtBytes(a.subtotal_bytes)}</td>
      </tr>
    `;
  }

  return `
    ${headline}
    <table style="width:100%;border-collapse:collapse;font-size:0.82rem">
      <thead>
        <tr style="text-align:left;color:var(--text3);font-size:0.72rem;text-transform:uppercase;letter-spacing:0.04em;border-bottom:1px solid var(--border)">
          <th style="padding:6px 8px;width:18px"></th>
          <th style="padding:6px 8px">App</th>
          <th style="padding:6px 8px;text-align:right">bot_guidance</th>
          <th style="padding:6px 8px;text-align:right">INSTALLED_APPS entry</th>
          <th style="padding:6px 8px;text-align:right">tool defs</th>
          <th style="padding:6px 8px;text-align:right">Subtotal</th>
        </tr>
      </thead>
      <tbody>
        ${apps.map(rowFor).join('')}
      </tbody>
    </table>
    <div style="font-size:0.7rem;color:var(--text3);margin-top:8px;line-height:1.5">
      Calibration phase: thresholds are reported, not enforced. Per-app guidance ≤ ${_cmFmtBytes(thresholds.bot_guidance_bytes_per_app)},
      per-app subtotal ≤ ${_cmFmtBytes(thresholds.per_app_subtotal_bytes)}. See
      <a href="https://github.com/evolve-ops/evolve/blob/main/docs/principle-apps-minimize-bootstrap-cost.md" target="_blank" rel="noopener" style="color:var(--accent)">principle-apps-minimize-bootstrap-cost</a>.
    </div>
  `;
}

function _cmRenderScore(d) {
  const grade = d.grade || 'F';
  const score = d.score ?? 0;
  const window_ = d.window_days || 7;
  const color = _CM_GRADE_COLORS[grade] || '#6b7280';
  const bg    = _CM_GRADE_BG[grade]    || 'rgba(107,114,128,0.12)';
  const components = d.components || [];

  // Each component renders its name + score/bar + detail. When the
  // component carries fix-field-keys we wire each as a clickable link
  // that scrolls the matrix row above into view and briefly outlines it
  // — the matrix is the action surface; this card is its diagnostic.
  function renderFixLinks(c) {
    const links = [];
    if (Array.isArray(c.fix_field_keys)) {
      for (const k of c.fix_field_keys) {
        links.push(`<a href="#" onclick="_cmJumpToField('${escHtml(k)}'); return false;" class="cm-fix-link">${escHtml(_cmFieldLabel(k))}</a>`);
      }
    }
    if (c.fix_page === 'ai-optimization') {
      links.push(`<a href="#" onclick="nav(document.querySelector('.nav-item[data-page=&quot;ai-optimization&quot;]')); return false;" class="cm-fix-link">AI Optimization →</a>`);
    }
    return links.length ? `<div class="cm-fix-links">${links.join(' &nbsp;·&nbsp; ')}</div>` : '';
  }

  function renderComp(c) {
    const icon  = c.pass ? '✓' : '!';
    const iconCls = c.pass ? 'cm-comp-icon-ok' : 'cm-comp-icon-warn';
    const cscore = c.score ?? 0;
    const cmax   = c.max ?? 0;
    const pct    = cmax ? Math.round(cscore / cmax * 100) : 0;
    const barFill = c.pass ? 'var(--accent)' : 'var(--yellow)';
    const bar = `<div class="cm-comp-bar"><div class="cm-comp-bar-fill" style="width:${Math.max(pct, 2)}%;background:${barFill}"></div></div>`;
    const detail = c.detail ? `<span class="cm-comp-detail">${escHtml(c.detail)}</span>` : '';
    const fix = c.fix ? `<div class="cm-comp-fix">${escHtml(c.fix)}</div>` : '';
    const fixLinks = !c.pass ? renderFixLinks(c) : '';
    return `<div class="cm-comp">
      <div class="cm-comp-head">
        <span class="${iconCls}">${icon}</span>
        <b class="cm-comp-name">${escHtml(c.name)}</b>
        <span class="cm-comp-score">${cscore}/${cmax}</span>
      </div>
      ${bar}
      ${detail}
      ${fix}
      ${fixLinks}
    </div>`;
  }

  const passing = components.filter(c => c.pass).length;
  const failing = components.length - passing;

  return `
    <style>
      .cm-grade-row { display:flex; gap:24px; align-items:center; flex-wrap:wrap; }
      .cm-grade-badge { width:84px; height:84px; border-radius:50%; display:flex; flex-direction:column; align-items:center; justify-content:center; flex-shrink:0; }
      .cm-grade-letter { font-size:1.9rem; font-weight:800; line-height:1; }
      .cm-grade-score  { font-size:0.8rem; font-weight:600; margin-top:2px; }
      .cm-grade-label  { font-size:0.95rem; font-weight:600; color:var(--text); }
      .cm-grade-sub    { font-size:0.76rem; color:var(--text2); margin-top:2px; }
      .cm-grade-window { font-size:0.7rem; color:var(--text3); margin-top:6px; }
      .cm-comp-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:14px 20px; margin-top:16px; padding-top:14px; border-top:1px solid var(--border); }
      .cm-comp { font-size:0.82rem; }
      .cm-comp-head { display:flex; align-items:baseline; gap:6px; margin-bottom:4px; }
      .cm-comp-icon-ok   { color:var(--green); font-weight:700; }
      .cm-comp-icon-warn { color:var(--yellow); font-weight:700; }
      .cm-comp-name  { font-size:0.82rem; }
      .cm-comp-score { font-size:0.72rem; color:var(--text2); margin-left:auto; }
      .cm-comp-bar { width:100%; height:3px; background:var(--border); border-radius:2px; overflow:hidden; margin-bottom:4px; }
      .cm-comp-bar-fill { height:100%; transition:width 0.2s; }
      .cm-comp-detail { font-size:0.72rem; color:var(--text3); }
      .cm-comp-fix { font-size:0.72rem; color:var(--text2); margin-top:4px; line-height:1.45; }
      .cm-fix-links { font-size:0.72rem; margin-top:4px; }
      .cm-fix-link  { color:var(--accent); text-decoration:none; }
      .cm-fix-link:hover { text-decoration:underline; }
      .cm-row-flash { animation:cmFlash 1.6s ease-out; }
      @keyframes cmFlash {
        0%   { box-shadow:inset 0 0 0 2px rgba(127,200,255,0.0); }
        20%  { box-shadow:inset 0 0 0 2px rgba(127,200,255,0.7); }
        100% { box-shadow:inset 0 0 0 2px rgba(127,200,255,0.0); }
      }
    </style>
    <div class="cm-grade-row">
      <div class="cm-grade-badge" style="background:${bg};border:3px solid ${color}">
        <div class="cm-grade-letter" style="color:${color}">${grade}</div>
        <div class="cm-grade-score"  style="color:${color}">${score}/100</div>
      </div>
      <div style="flex:1;min-width:180px">
        <div class="cm-grade-label">${escHtml(d.label || '')}</div>
        <div class="cm-grade-sub">${passing} of ${components.length} components passing${failing ? ` — ${failing} flagged below` : ''}</div>
        <div class="cm-grade-window">behavioral measurement over last ${window_} day${window_ === 1 ? '' : 's'} · cap + setting matrices below show what's configured</div>
      </div>
    </div>
    <div class="cm-comp-grid">
      ${components.map(renderComp).join('')}
    </div>`;
}

// Map a matrix field key back to its short label for fix-link text.
function _cmFieldLabel(key) {
  const f = (typeof _CM_FIELDS !== 'undefined') ? _CM_FIELDS.find(x => x.key === key) : null;
  return f ? f.label : key;
}

// Scroll the matrix row for `fieldKey` into view and briefly flash it.
// The score card is below the matrix, so jumping up is the common path.
function _cmJumpToField(fieldKey) {
  const row = document.querySelector(`#cm-settings-body tr [data-field="${CSS.escape(fieldKey)}"]`);
  if (!row) return;
  const tr = row.closest('tr');
  if (!tr) return;
  tr.scrollIntoView({ behavior: 'smooth', block: 'center' });
  // Force-restart the animation if the row was flashed recently.
  tr.classList.remove('cm-row-flash');
  // void offsetWidth — reflow trick so the class re-add triggers animation
  void tr.offsetWidth;
  tr.classList.add('cm-row-flash');
}

// ── Section 2: Context & Session Settings (matrix) ───────────────────────────
//
// One unified matrix replaces the prior form-fields + Cost Profiles cards.
// Rows are individual cost-relevant openclaw settings (with descriptions);
// columns are the three built-in profiles (Conservative / Balanced /
// Performance) plus an "Other" column whose data source is chosen from a
// dropdown — Custom (the bot's current values, read-only) or any saved
// profile. Click a column header to apply the whole profile via
// /api/bot/apply-profile; click a single cell to PATCH just that one field
// via /api/bot/openclaw-cost.
//
// The active value on each row is highlighted in whichever column it lives
// in, so the operator can see at a glance "where I am right now."

const _CM_FIELDS = [
  { group: 'Heartbeat',
    key: 'heartbeat.isolatedSession', label: 'Session Isolation',
    desc: "Each heartbeat (idle check-in) starts a fresh session instead of resuming the last one. Prevents a long-idle session from accumulating context you'll pay to re-process on the next real message.",
    type: 'bool' },
  { group: 'Heartbeat',
    key: 'heartbeat.lightContext', label: 'Light Context Mode',
    desc: 'Heartbeat turns inject only a minimal system prompt, skipping memory files and tools. A heartbeat that takes 200 tokens instead of 8,000 costs ~97% less.',
    type: 'bool' },
  { group: 'Context pruning',
    key: 'contextPruning.mode', label: 'Pruning mode',
    desc: 'Whether OpenClaw drops old session data from memory on a timer. Pruning reduces the context size the bot carries, lowering per-turn cost on long-running sessions.',
    type: 'enum',
    options: { '': 'Off', 'cache-ttl': 'Cache TTL', 'aggressive': 'Aggressive' } },
  { group: 'Context pruning',
    key: 'contextPruning.ttl', label: 'Pruning TTL',
    // NOT the prompt-cache TTL — that's the "Prompt-cache TTL" row below.
    // OpenClaw SKIPS pruning while the cache is still warm, so this is
    // "how long to wait before pruning is free", and the coherent value is
    // whatever the prompt cache is set to (5m short / 1h long). Above 1h it
    // is incoherent under every cache setting — see PR #3497 and
    // cost_profiles.MAX_COHERENT_PRUNE_TTL_SECONDS.
    desc: 'How long an idle session waits before its context is trimmed. Pruning rewrites the prefix, so OpenClaw holds off until the prompt cache has expired — set this to match the Prompt-cache TTL below (5 min for short, 1 hour for long). Longer than the cache means tool results ride untrimmed and get re-written at every cache expiry.',
    type: 'enum',
    options: { '': '—', '5m': '5 min', '15m': '15 min', '30m': '30 min', '1h': '1 hour', '4h': '4 hours' } },
  { group: 'Context pruning',
    key: 'contextPruning.keepLastAssistants', label: 'Keep last N turns',
    desc: "After pruning, keep this many of the most recent assistant turns in memory. Provides continuity so the bot isn't completely blank — it remembers the end of the last conversation.",
    type: 'int' },
  { group: 'Compaction',
    key: 'compaction.mode', label: 'Compaction mode',
    desc: 'Compaction summarizes the oldest parts of a growing context window to make room for new turns, instead of failing when the window is full.',
    type: 'enum',
    options: { '': 'Off', 'safeguard': 'Safeguard', 'aggressive': 'Aggressive' } },
  { group: 'Compaction',
    key: 'compaction.reserveTokensFloor', label: 'Reserve floor (tokens)',
    desc: 'OpenClaw keeps at least this many tokens free in the context window before triggering compaction. Higher = compaction triggers sooner = more aggressive but safer.',
    type: 'int' },
  { group: 'Compaction',
    key: 'compaction.memoryFlush.softThresholdTokens', label: 'Memory flush threshold (tokens)',
    desc: 'If memory files injected at session start exceed this size, older memory entries are flushed before injection. Prevents bloated memory files from eating context before the conversation begins.',
    type: 'int',
    // Setting non-null also flips enabled=true; nulling drops the whole sub-object.
    memoryFlushAutoEnable: true },
  { group: 'Session',
    key: 'bootstrapTotalMaxChars', label: 'Bootstrap max chars',
    desc: 'Hard limit on total characters injected from workspace files at session start (memory, tools, SOUL.md, etc.). Older/lower-priority files are trimmed when exceeded.',
    type: 'int' },
  { group: 'Session',
    key: 'session.reset.idleMinutes', label: 'Idle reset (minutes)',
    desc: 'After this many idle minutes, the session is fully reset — the next message starts fresh.',
    type: 'int' },
  { group: 'Optimization',
    key: 'cache_retention', label: 'Prompt-cache TTL',
    // cache_retention isn't an openclaw.json field — it lives in BE config
    // (better-engine-config.json::bots.<bot>.budget.per_bot_cache_retention).
    // The server's GET /api/bot/openclaw-cost merges it into the settings
    // response and the PATCH endpoint routes it back to BE config, so the
    // matrix doesn't need any special-case path handling.
    // The ONLY knob that changes prompt-cache lifetime — the Pruning TTL row
    // above does not, it just gates when pruning may run.
    desc: "Anthropic prompt-cache TTL applied to every Anthropic model, and the only setting that changes how long a cached prefix lives. short = 5-min (writes cost 1.25x base input); long = 1-hour (writes cost 2x). Long only pays off when the prefix would otherwise be re-written more than ~1.6 times an hour — it eliminates invalidations on multi-minute conversational gaps, but a bot whose turns are hours apart breaks a 1-hour cache just as often and simply pays the higher write.",
    type: 'enum',
    options: { '': '— (OC default: short)', 'short': 'short (5-min)', 'long': 'long (1-hour)' } },
];

// Matrix state — populated by _cmLoadSettings, consumed by _cmRenderMatrix.
let _cmCurrentSettings = null;       // bot's current values (deep object)
let _cmAvailableProfiles = [];        // builtin + saved, from /api/cost-profiles
let _cmOtherSource = '';              // '' = Custom (current); else saved-profile name

async function _cmLoadSettings() {
  const el = document.getElementById('cm-settings-body');
  const lbl = document.getElementById('cm-settings-bot-label');
  if (!el) return;
  if (!_cmBot) { el.innerHTML = '<div class="empty">Select a bot above.</div>'; return; }
  if (lbl) lbl.textContent = `— ${_cmBot}`;
  el.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';
  try {
    // Pull settings + profiles in parallel — the matrix needs both.
    const [s, p] = await Promise.all([
      api('GET', `/api/bot/openclaw-cost?bot=${encodeURIComponent(_cmBot)}`),
      api('GET', '/api/cost-profiles'),
    ]);
    _cmCurrentSettings = s.settings || {};
    _cmAvailableProfiles = p.profiles || [];
    // If the user's previous Other selection is gone (deleted in another tab),
    // fall back to Custom rather than render a phantom column.
    if (_cmOtherSource && !_cmAvailableProfiles.some(pf => pf.name === _cmOtherSource)) {
      _cmOtherSource = '';
    }
    el.innerHTML = _cmRenderMatrix();
  } catch(e) {
    el.innerHTML = `<div class="empty">Could not load settings: ${escHtml(String(e))}</div>`;
  }
}

async function _cmRefreshSettings() { await _cmLoadSettings(); }

// ── Matrix helpers ──

function _cmGetByPath(obj, path) {
  if (!obj) return undefined;
  return path.split('.').reduce((o, k) => (o == null ? o : o[k]), obj);
}

function _cmCellDisplay(field, val) {
  // Booleans render as explicit Enabled/Disabled labels rather than ✓/—.
  // The glyphs were ambiguous (does a dash mean "off" or "unset"?) and
  // mismatched the caps matrix below which uses Off/Default/Custom chips.
  if (field.type === 'bool') return val === true ? 'Enabled' : 'Disabled';
  if (val == null || val === '') {
    // Enum with an explicit "empty" label (e.g. 'Off' for null/empty) wins;
    // otherwise fall back to the unset glyph.
    if (field.type === 'enum' && field.options && field.options[''] !== undefined) return field.options[''];
    return '—';
  }
  if (field.type === 'enum') return (field.options && field.options[val]) || String(val);
  if (field.type === 'int') return Number(val).toLocaleString();
  return String(val);
}

function _cmValuesEqual(a, b) {
  // null/undefined/'' all considered "unset" for comparison purposes — a
  // missing field and an explicit null read as the same active state.
  const norm = v => (v == null || v === '') ? null : v;
  return norm(a) === norm(b);
}

function _cmBuildPatch(field, val) {
  // Special-case: memory flush threshold lives inside compaction.memoryFlush
  // and is paired with an `enabled` flag. Setting it implies enabling; nulling
  // it drops the whole sub-object (cleaner than partial null + enabled=false).
  if (field.memoryFlushAutoEnable) {
    if (val == null) return { compaction: { memoryFlush: null } };
    return { compaction: { memoryFlush: { enabled: true, softThresholdTokens: val } } };
  }
  // Generic dotted-key → nested object.
  const parts = field.key.split('.');
  const root = {};
  let cur = root;
  for (let i = 0; i < parts.length - 1; i++) {
    cur[parts[i]] = {};
    cur = cur[parts[i]];
  }
  cur[parts[parts.length - 1]] = val;
  return root;
}

// Per-row description-expand state for both matrices. Each set holds the
// field keys whose descriptions are currently expanded; everything else
// is collapsed. Module-level so it persists across re-renders triggered
// by cell clicks, refresh, or bot switches. Default empty (everything
// collapsed) so the table is easy to scan.
//
// Operator interactions:
//   - Per-row chevron toggles just that one row.
//   - Header button "Expand all" / "Collapse all" toggles every row at
//     once, label flipping based on whether any rows are currently
//     expanded. This replaces the prior boolean "Show descriptions"
//     toggle, which couldn't preserve per-row state.
const _cmExpandedSettingsRows = new Set();
const _cmExpandedCapsRows = new Set();

function _cmToggleSettingsRow(fieldKey) {
  if (_cmExpandedSettingsRows.has(fieldKey)) _cmExpandedSettingsRows.delete(fieldKey);
  else _cmExpandedSettingsRows.add(fieldKey);
  const el = document.getElementById('cm-settings-body');
  if (el && _cmCurrentSettings) el.innerHTML = _cmRenderMatrix();
}

function _cmToggleAllSettingsRows() {
  if (_cmExpandedSettingsRows.size > 0) {
    _cmExpandedSettingsRows.clear();
  } else {
    for (const f of _CM_FIELDS) _cmExpandedSettingsRows.add(f.key);
  }
  const el = document.getElementById('cm-settings-body');
  if (el && _cmCurrentSettings) el.innerHTML = _cmRenderMatrix();
}

function _cmToggleCapsRow(scope, fieldKey) {
  if (_cmExpandedCapsRows.has(fieldKey)) _cmExpandedCapsRows.delete(fieldKey);
  else _cmExpandedCapsRows.add(fieldKey);
  const el = document.getElementById(scope === 'per-bot' ? 'cm-bot-caps-body' : 'cm-pod-caps-body');
  if (el) el.innerHTML = _cmRenderCapsMatrix(scope);
}

function _cmToggleAllCapsRows(scope) {
  // Match the per-scope row filter from _cmRenderCapsMatrix so the global
  // toggle's "expanded set" exactly covers the rows actually visible.
  const visible = _CM_CAP_FIELDS.filter(f => scope === 'pod-default' || !f.podOnly);
  // Use the visible-row set to decide collapse-vs-expand so the chevron
  // header label matches what the operator sees in this scope. A row
  // expanded on the per-bot tab + collapsed on POD is still consistent
  // because they read from the same set — but the "all" decision is per
  // visible row count, which is what the operator expects.
  const allExpanded = visible.every(f => _cmExpandedCapsRows.has(f.key));
  if (allExpanded) {
    for (const f of visible) _cmExpandedCapsRows.delete(f.key);
  } else {
    for (const f of visible) _cmExpandedCapsRows.add(f.key);
  }
  const el = document.getElementById(scope === 'per-bot' ? 'cm-bot-caps-body' : 'cm-pod-caps-body');
  if (el) el.innerHTML = _cmRenderCapsMatrix(scope);
}

// Parse an OC heartbeat interval ("5m", "1h", "30s") to seconds. Mirrors
// cost_profiles._parse_every_to_seconds — same null-on-unparseable contract.
function _cmParseEveryToSeconds(every) {
  if (typeof every !== 'string' || !every) return null;
  const s = every.trim().toLowerCase();
  if (!s) return null;
  const unit = s[s.length - 1];
  const n = parseInt(s.slice(0, -1), 10);
  if (!Number.isFinite(n)) return null;
  if (unit === 's') return n;
  if (unit === 'm') return n * 60;
  if (unit === 'h') return n * 3600;
  if (unit === 'd') return n * 86400;
  return null;
}

// Mirror of cost_profiles.preflight_heartbeat_combination — client-side
// detection so the operator gets red visual treatment before a save lands a
// 400. Returns null when the settings are acceptable, or { message,
// suggestedFixes } when the wasteful combo is active. Keep this logic in
// lockstep with the backend gate; divergence means the UI flags something
// the server would accept (or vice versa).
function _cmDetectBadHeartbeatCombo(settings) {
  if (!settings) return null;
  const hb = settings.heartbeat || {};
  if (hb.lightContext !== false) return null;
  const everyS = _cmParseEveryToSeconds(hb.every);
  if (everyS == null || everyS < 60 * 60) return null;
  return {
    message: (
      `Heartbeat is set to lightContext=false at every=${hb.every || '(unset)'} — ` +
      `Anthropic's prompt cache (5 min default, 1 h max) expires between ticks, ` +
      `so every heartbeat pays full input price for zero cache benefit.`
    ),
    suggestedFixes: [
      {
        label: '✓ Switch lightContext to true',
        patch: { heartbeat: { lightContext: true } },
        statusLabel: 'lightContext',
      },
      {
        label: 'Apply Balanced profile',
        applyProfile: 'balanced',
      },
    ],
  };
}

// Apply a quick-fix patch from the bad-combo banner. The patch goes through
// the same PATCH endpoint a cell click does — the banner is just a more
// discoverable affordance for the common remediation. Errors surface inline
// where the banner sits, not a transient toast.
async function _cmApplyQuickFix(fixIdx) {
  if (!_cmBot) return;
  const combo = _cmDetectBadHeartbeatCombo(_cmCurrentSettings);
  if (!combo) return;
  const fix = combo.suggestedFixes[fixIdx];
  if (!fix) return;
  const banner = document.getElementById('cm-bad-combo-banner');
  const st = document.getElementById('cm-settings-status');
  if (st) st.innerHTML = `<span class="spinner" style="width:12px;height:12px;display:inline-block"></span> Applying fix…`;
  try {
    if (fix.applyProfile) {
      const r = await api('PATCH', `/api/bot/apply-profile?bot=${encodeURIComponent(_cmBot)}`, { profile: fix.applyProfile });
      if (!r.ok) throw new Error(r.error || 'apply-profile failed');
    } else {
      await api('PATCH', `/api/bot/openclaw-cost?bot=${encodeURIComponent(_cmBot)}`, fix.patch);
    }
    if (st) st.innerHTML = `<span style="color:var(--green)">✓ Heartbeat configuration repaired.</span>`;
    if (banner) banner.style.display = 'none';
    await _cmLoadSettings();
    setTimeout(() => _cmLoadScore(), 400);
  } catch(e) {
    if (st) st.innerHTML = `<span style="color:var(--danger)">Fix failed: ${escHtml(String(e))}</span>`;
  }
}

function _cmRenderMatrix() {
  if (!_cmCurrentSettings) return '<div class="empty">No settings loaded.</div>';
  const profileByName = Object.fromEntries(_cmAvailableProfiles.map(p => [p.name, p]));
  const builtinOrder = ['conservative', 'balanced', 'unrestricted-debug'];
  const badCombo = _cmDetectBadHeartbeatCombo(_cmCurrentSettings);
  const builtinCols = builtinOrder.map(name => profileByName[name]).filter(Boolean);

  // Other column source: '' = Custom (use current settings), else saved profile.
  const otherProfile = _cmOtherSource ? profileByName[_cmOtherSource] : null;
  const otherSettings = otherProfile ? otherProfile.settings : _cmCurrentSettings;
  const otherIsCustom = !otherProfile;
  const otherDeletable = otherProfile && !otherProfile.builtin;
  const savedProfiles = _cmAvailableProfiles.filter(p => !p.builtin);

  const colCount = 1 + builtinCols.length + 1; // setting + builtins + other
  // Global header label flips based on whether ANY rows are currently
  // expanded — operator can collapse the lot with one click if they
  // expanded a few and got distracted, or expand everything to scan
  // descriptions cover-to-cover.
  const anyExpanded = _cmExpandedSettingsRows.size > 0;

  function colHeader(label, profileName, isApplyable, extraTopHtml) {
    // The whole header is the click-target when applyable — keeps the
    // visual tight (no extra button row) and the hover-state hint makes
    // the affordance obvious. Cells handle per-row clicks below.
    //
    // The unrestricted-debug column carries an additional warning style:
    // the profile is legitimate in narrow debug contexts but pure waste
    // at every other interval. Visual cue says "stop and think" before
    // the operator clicks; the confirm dialog in _cmHeaderApply seals it.
    const isUnrestricted = profileName === 'unrestricted-debug';
    const cls = [
      'cm-col-head',
      isApplyable ? 'cm-col-head-apply' : '',
      isUnrestricted ? 'cm-col-head-warn' : '',
    ].filter(Boolean).join(' ');
    const onclick = isApplyable
      ? `onclick="_cmHeaderApply('${escHtml(profileName)}')"`
      : '';
    const labelHtml = isUnrestricted
      ? `<div class="cm-col-label"><span style="color:#fca5a5;margin-right:4px">⚠</span>${escHtml(label)}</div>`
      : `<div class="cm-col-label">${escHtml(label)}</div>`;
    return `<th class="${cls}" ${onclick}>
      ${labelHtml}
      ${extraTopHtml || ''}
      ${isApplyable ? '<div class="cm-apply-hint">click to apply →</div>' : ''}
    </th>`;
  }

  // Other column header: dropdown (Custom + saved profiles) + Delete (only
  // shown for custom saved profiles). The dropdown is non-interactive for
  // the column-apply click; stopPropagation keeps clicks on the dropdown
  // from triggering the header's apply handler.
  const otherDropdownOpts = [
    `<option value="" ${_cmOtherSource === '' ? 'selected' : ''}>Custom (current)</option>`,
    ...savedProfiles.map(p => `<option value="${escHtml(p.name)}" ${_cmOtherSource === p.name ? 'selected' : ''}>${escHtml(p.label || p.name)}</option>`),
  ].join('');
  const otherHeaderExtra = `
    <select id="cm-other-source"
            onchange="_cmOtherChange(this.value)"
            onclick="event.stopPropagation()"
            class="cm-other-select input-w-md">${otherDropdownOpts}</select>
    ${otherDeletable
      ? `<button class="btn btn-ghost btn-sm cm-other-del" onclick="event.stopPropagation();_cmDeleteOtherProfile()">🗑 Delete</button>`
      : ''}`;

  function cellHtml(field, sourceSettings, profileName, isInteractive, isCustomColumn) {
    const val = _cmGetByPath(sourceSettings, field.key);
    const cur = _cmGetByPath(_cmCurrentSettings, field.key);
    const isActive = _cmValuesEqual(val, cur);
    const isUnset = (val == null || val === '');

    // Editable Custom column for int fields: render an inline number input
    // so the operator can type any value rather than being limited to the
    // three preset profile columns. Booleans + enums stay non-editable in
    // Custom — the preset columns already cover every valid value, so an
    // inline editor would be redundant clutter.
    if (isCustomColumn && field.type === 'int') {
      const valStr = (val == null || val === '') ? '' : String(val);
      // Active highlight + unset styling still apply via classes — only the
      // interactive click-handler is swapped for the input's commit handler.
      const cls = [
        'cm-cell', 'cm-cell-custom-editable',
        isActive ? 'cm-cell-active' : '',
        isUnset ? 'cm-cell-unset' : '',
      ].filter(Boolean).join(' ');
      return `<td class="${cls}" data-field="${escHtml(field.key)}">
        <input type="number" min="0" step="1" placeholder="—" value="${escHtml(valStr)}"
               class="cm-cell-custom-input"
               data-field="${escHtml(field.key)}"
               onkeydown="if(event.key==='Enter'){event.target.blur();}"
               onblur="_cmCellCustomCommit(this)">
      </td>`;
    }

    const display = _cmCellDisplay(field, val);
    // Bad-combo highlight: the two heartbeat fields that compose the
    // wasteful combo get a red border on the row showing the bot's
    // CURRENT value, so the operator's eye lands on the cells they need
    // to change. Doesn't affect profile-column cells — only the Custom
    // column's current-state cells need flagging.
    const isBadComboField = badCombo && (
      field.key === 'heartbeat.lightContext' || field.key === 'heartbeat.every'
    );
    const isShowingCurrentValue = _cmValuesEqual(val, cur);
    const flagBadCell = isBadComboField && isShowingCurrentValue;
    const cls = [
      'cm-cell',
      isActive ? 'cm-cell-active' : '',
      isInteractive ? 'cm-cell-interactive' : '',
      isUnset ? 'cm-cell-unset' : '',
      flagBadCell ? 'cm-cell-bad-combo' : '',
    ].filter(Boolean).join(' ');
    const onclick = isInteractive
      ? `onclick="_cmCellClick('${escHtml(field.key)}','${escHtml(profileName)}')"`
      : '';
    return `<td class="${cls}" data-field="${escHtml(field.key)}" ${onclick}>${escHtml(display)}</td>`;
  }

  let lastGroup = null;
  const rows = _CM_FIELDS.map(field => {
    let groupRow = '';
    if (field.group !== lastGroup) {
      groupRow = `<tr class="cm-group-row"><td colspan="${colCount}" class="cm-group-label">${escHtml(field.group)}</td></tr>`;
      lastGroup = field.group;
    }
    const isExpanded = _cmExpandedSettingsRows.has(field.key);
    const descHtml = isExpanded
      ? `<div class="cm-setting-desc">${escHtml(field.desc)}</div>`
      : '';
    // Per-row chevron — inline next to the label. Uses .expand-icon
    // (style-guide §9.13); the .is-open class rotates it when expanded.
    // The whole label-row is a clickable target so the operator doesn't
    // have to hit the tiny glyph precisely; clicking anywhere on the
    // label row toggles its description.
    const labelRow = `<button type="button" class="cm-setting-label-row"
      onclick="_cmToggleSettingsRow('${escHtml(field.key)}')"
      title="${isExpanded ? 'Hide' : 'Show'} description">
      <span class="cm-setting-chevron expand-icon${isExpanded ? ' is-open' : ''}" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
      <span class="cm-setting-label">${escHtml(field.label)}</span>
    </button>`;
    const settingCell = `<td class="cm-setting-cell">
      ${labelRow}
      ${descHtml}
    </td>`;
    const builtinCells = builtinCols.map(p => cellHtml(field, p.settings, p.name, true, false)).join('');
    // Other column is "Custom" only when no saved profile is selected.
    const otherCell = cellHtml(field, otherSettings, _cmOtherSource, !otherIsCustom, otherIsCustom);
    return groupRow + `<tr>${settingCell}${builtinCells}${otherCell}</tr>`;
  }).join('');

  return `
    <style>
      .cm-matrix { width:100%; border-collapse:separate; border-spacing:0; }
      .cm-matrix th, .cm-matrix td { vertical-align:middle; }
      .cm-matrix th { padding:8px; text-align:center; background:var(--bg2); border-bottom:1px solid var(--border); }
      .cm-matrix th:first-child { text-align:left; font-size:0.72rem; color:var(--text3); text-transform:uppercase; letter-spacing:0.05em; background:transparent; }
      .cm-col-label { font-weight:600; font-size:0.85rem; color:var(--text); }
      .cm-apply-hint { font-size:0.68rem; color:var(--text3); margin-top:2px; }
      .cm-col-head-apply { cursor:pointer; transition:background 0.1s; }
      .cm-col-head-apply:hover { background:var(--bg3); }
      .cm-col-head-apply:hover .cm-apply-hint { color:var(--accent); }
      /* Unrestricted-debug profile — visual warn so the operator pauses
         before the click. Doesn't disable the column; the confirm()
         dialog in _cmHeaderApply is the actual blocking gate. */
      .cm-col-head-warn { background:rgba(239,68,68,0.10); border-bottom:1px solid rgba(239,68,68,0.45); }
      .cm-col-head-warn:hover { background:rgba(239,68,68,0.18); }
      .cm-col-head-warn .cm-col-label { color:#fca5a5; }
      .cm-other-select { font-size:0.74rem; padding:2px 6px; background:var(--bg3); border:1px solid var(--border); color:var(--text); border-radius:4px; margin-top:4px; max-width:160px; cursor:pointer; }
      .cm-other-del { font-size:0.7rem; padding:1px 6px; margin-top:3px; color:var(--danger); display:block; margin-left:auto; margin-right:auto; }
      .cm-cell { padding:7px 10px; text-align:center; font-size:0.85rem; background:rgba(255,255,255,0.01); border-bottom:1px solid rgba(255,255,255,0.04); }
      .cm-cell-interactive { cursor:pointer; transition:background 0.1s; }
      .cm-cell-interactive:hover { background:rgba(127,200,255,0.10); }
      .cm-cell-active { font-weight:700; color:var(--accent); background:rgba(127,200,255,0.12); box-shadow:inset 0 0 0 1px rgba(127,200,255,0.35); }
      .cm-cell-unset { color:var(--text3); }
      /* Bad-combo flag — overrides cell-active because the operator needs to
         SEE the offending values even when they're the bot's current state.
         The red border tracks every render of the matrix while the combo is
         live; clears the moment the combo is repaired. */
      .cm-cell-bad-combo { background:rgba(239,68,68,0.14); box-shadow:inset 0 0 0 2px rgba(239,68,68,0.55); color:var(--text); font-weight:700; }
      .cm-cell-bad-combo::after { content:' ⚠'; color:rgba(239,68,68,0.95); }
      .cm-cell-custom-editable { padding:4px 6px; }
      .cm-cell-custom-input { width:100%; max-width:90px; text-align:center; background:transparent; border:1px solid var(--border); border-radius:4px; color:var(--text); font-size:0.85rem; padding:4px 6px; outline:none; -moz-appearance:textfield; }
      .cm-cell-custom-input:focus { border-color:rgba(127,200,255,0.55); }
      .cm-cell-custom-input::-webkit-outer-spin-button,
      .cm-cell-custom-input::-webkit-inner-spin-button { -webkit-appearance:none; margin:0; }
      .cm-cell-custom-input::placeholder { color:var(--text3); }
      .cm-group-row td { background:transparent; }
      .cm-group-label { padding:12px 8px 4px; font-size:0.7rem; color:var(--text3); text-transform:uppercase; letter-spacing:0.05em; font-weight:600; border-top:1px solid var(--border); }
      .cm-setting-cell { padding:7px 14px 7px 8px; vertical-align:top; background:transparent; border-bottom:1px solid rgba(255,255,255,0.04); }
      .cm-setting-label { font-weight:600; font-size:0.82rem; color:var(--text); }
      .cm-setting-desc { font-size:0.72rem; color:var(--text3); margin-top:4px; line-height:1.4; max-width:340px; padding-left:14px; }
      .cm-setting-label-row { display:flex; align-items:baseline; gap:6px; background:transparent; border:none; padding:0; margin:0; cursor:pointer; text-align:left; color:inherit; font:inherit; width:100%; }
      .cm-setting-label-row:hover .cm-setting-chevron { color:var(--accent); }
      .cm-setting-chevron { font-size:0.7rem; color:var(--text3); width:10px; flex-shrink:0; transition:color 0.1s; }
    </style>
    ${badCombo ? `
    <div id="cm-bad-combo-banner" style="margin-bottom:12px;padding:12px 14px;border-radius:6px;background:rgba(239,68,68,0.14);border:1px solid rgba(239,68,68,0.55);display:flex;gap:14px;align-items:center;flex-wrap:wrap">
      <div style="font-size:1.4rem;line-height:1">⚠</div>
      <div style="flex:1;min-width:240px">
        <div style="font-weight:700;color:#fca5a5;margin-bottom:3px">Wasteful heartbeat configuration</div>
        <div style="font-size:0.8rem;color:var(--text);line-height:1.45">${escHtml(badCombo.message)}</div>
        <div style="font-size:0.72rem;color:var(--text3);margin-top:4px">See <a href="https://github.com/evolve-ops/evolve/blob/main/docs/principle-apps-minimize-bootstrap-cost.md" target="_blank" rel="noopener" style="color:var(--accent)">principle-apps-minimize-bootstrap-cost</a>.</div>
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        ${badCombo.suggestedFixes.map((f, i) => `
          <button class="btn btn-sm" style="background:#dc2626;color:#fff;border:none;white-space:nowrap;font-size:0.78rem" onclick="_cmApplyQuickFix(${i})">${escHtml(f.label)}</button>
        `).join('')}
      </div>
    </div>` : ''}
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;gap:12px;flex-wrap:wrap">
      <div style="font-size:0.78rem;color:var(--text3);flex:1;min-width:200px">Click a column header to apply the whole profile; click a single cell to change just that one setting. <strong style="color:var(--accent)">Highlighted</strong> values match what's currently configured on this bot.</div>
      <div style="display:flex;gap:6px;align-items:center">
        <button class="btn btn-ghost btn-sm" style="white-space:nowrap;font-size:0.74rem" onclick="_cmToggleAllSettingsRows()">${anyExpanded ? 'Collapse all' : 'Expand all'}</button>
        <button class="btn btn-ghost btn-sm" style="white-space:nowrap" onclick="_evoDrawerOpen({prefill: 'Explain the context and session settings on the Cost Optimization page — what do compaction, pruning, TTL, reserve floor, and bootstrap max mean in plain terms?'})">✦ Ask evo</button>
      </div>
    </div>
    <div style="overflow-x:auto">
      <table class="cm-matrix">
        <thead>
          <tr>
            <th>Setting</th>
            ${builtinCols.map(p => colHeader(p.label || p.name, p.name, true)).join('')}
            ${colHeader('Other', _cmOtherSource, !otherIsCustom, otherHeaderExtra)}
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div id="cm-settings-status" style="margin-top:10px;font-size:0.78rem;min-height:1.1em"></div>`;
}

// ── Matrix interactions ──

// Commit an inline edit from the Custom column's number input. Triggered
// on blur or Enter. Empty input clears the override (treated as null).
// Compared with _cmCellClick (which copies a profile column's value),
// this lets the operator set arbitrary numbers — useful when none of the
// three preset profiles match what they want.
async function _cmCellCustomCommit(input) {
  if (!input || !_cmBot) return;
  const fieldKey = input.dataset.field;
  const field = _CM_FIELDS.find(f => f.key === fieldKey);
  if (!field) return;
  const raw = input.value.trim();
  const currentVal = _cmGetByPath(_cmCurrentSettings, fieldKey);
  let newVal;
  if (raw === '') {
    newVal = null;
  } else {
    const n = parseFloat(raw);
    if (!Number.isFinite(n) || n < 0) {
      const st = document.getElementById('cm-settings-status');
      if (st) st.innerHTML = `<span style="color:var(--danger)">${escHtml(field.label)} must be a non-negative number, or blank to clear.</span>`;
      return;
    }
    newVal = n;
  }
  // Skip no-op writes — blurring without changes shouldn't trip a refresh.
  if (_cmValuesEqual(newVal, currentVal)) return;
  const patch = _cmBuildPatch(field, newVal);
  const st = document.getElementById('cm-settings-status');
  if (st) st.innerHTML = `<span class="spinner" style="width:12px;height:12px;display:inline-block"></span> Updating ${escHtml(field.label)}…`;
  try {
    // api() returns the JSON body even on non-2xx — check the response
    // shape explicitly so a preflight 400 doesn't masquerade as success.
    const r = await api('PATCH', `/api/bot/openclaw-cost?bot=${encodeURIComponent(_cmBot)}`, patch);
    if (r && r.error) {
      _cmRenderInlineError(r.error);
      return;
    }
    if (st) st.innerHTML = `<span style="color:var(--green)">✓ ${escHtml(field.label)} set to ${newVal == null ? 'unset' : newVal.toLocaleString()}</span>`;
    await _cmLoadSettings();
    setTimeout(() => _cmLoadScore(), 400);
  } catch(e) {
    _cmRenderInlineError(String(e));
  }
}

async function _cmCellClick(fieldKey, profileName) {
  const field = _CM_FIELDS.find(f => f.key === fieldKey);
  if (!field || !_cmBot) return;
  if (!profileName) return;   // Custom column is read-only — no-op
  const profile = _cmAvailableProfiles.find(p => p.name === profileName);
  if (!profile) return;
  const val = _cmGetByPath(profile.settings, field.key);
  const patch = _cmBuildPatch(field, val == null ? null : val);
  const st = document.getElementById('cm-settings-status');
  if (st) st.innerHTML = `<span class="spinner" style="width:12px;height:12px;display:inline-block"></span> Updating ${escHtml(field.label)}…`;
  try {
    // Same shape-check as _cmCellCustomCommit — a preflight 400 returns
    // a body, not a thrown exception.
    const r = await api('PATCH', `/api/bot/openclaw-cost?bot=${encodeURIComponent(_cmBot)}`, patch);
    if (r && r.error) {
      _cmRenderInlineError(r.error);
      return;
    }
    if (st) st.innerHTML = `<span style="color:var(--green)">✓ ${escHtml(field.label)} updated from ${escHtml(profile.label || profile.name)}</span>`;
    await _cmLoadSettings();
    setTimeout(() => _cmLoadScore(), 400);
  } catch(e) {
    _cmRenderInlineError(String(e));
  }
}

// Inline-render an error in the status row. Preflight 400s contain a
// long-form rationale; a transient toast or one-line "Failed:" hides the
// remediation. When the error mentions "no valid use case" we know it's
// our gate firing — render the full message with the fix buttons inline,
// not just the bare string.
function _cmRenderInlineError(errMsg) {
  const st = document.getElementById('cm-settings-status');
  if (!st) return;
  const isPreflight = typeof errMsg === 'string' && errMsg.includes('no valid use case');
  if (!isPreflight) {
    st.innerHTML = `<span style="color:var(--danger)">Failed: ${escHtml(errMsg || 'unknown error')}</span>`;
    return;
  }
  st.innerHTML = `
    <div style="padding:10px 12px;border-radius:6px;background:rgba(239,68,68,0.12);border:1px solid rgba(239,68,68,0.5);margin-top:4px">
      <div style="font-weight:700;color:#fca5a5;margin-bottom:4px">Save blocked by preflight</div>
      <div style="font-size:0.8rem;color:var(--text);line-height:1.45">${escHtml(errMsg)}</div>
    </div>`;
}

async function _cmHeaderApply(profileName) {
  if (!_cmBot || !profileName) return;
  const profile = _cmAvailableProfiles.find(p => p.name === profileName);
  if (!profile) return;
  // Unrestricted-debug is legitimate in narrow debug contexts but the
  // operator needs to know what they're agreeing to. Confirm dialog
  // text echoes the profile description so the trade-off is visible
  // at the moment of decision — not after the 400 fires.
  if (profileName === 'unrestricted-debug') {
    const desc = profile.description || '';
    const ok = await confirmModal({body: (
      `Apply "${profile.label || profile.name}" to ${_cmBot}?\n\n` +
      desc + '\n\n' +
      'This skips the preflight gate that normally blocks lightContext=false at ' +
      'heartbeat intervals ≥ 5 minutes. A cost_profile_unrestricted_applied ' +
      'Signal will fire. Click OK only if you are actively debugging cache ' +
      'behavior at sub-5min cadence.'
    )});
    if (!ok) return;
  }
  const st = document.getElementById('cm-settings-status');
  if (st) st.innerHTML = `<span class="spinner" style="width:12px;height:12px;display:inline-block"></span> Applying ${escHtml(profile.label || profile.name)}…`;
  try {
    const r = await api('PATCH', `/api/bot/apply-profile?bot=${encodeURIComponent(_cmBot)}`, { profile: profileName });
    if (r.ok) {
      if (st) st.innerHTML = `<span style="color:var(--green)">✓ Applied ${escHtml(profile.label || profile.name)} to ${escHtml(_cmBot)}</span>`;
      await _cmLoadSettings();
      setTimeout(() => _cmLoadScore(), 400);
    } else {
      _cmRenderInlineError(r.error || 'unknown error');
    }
  } catch(e) {
    _cmRenderInlineError(String(e));
  }
}

function _cmOtherChange(value) {
  _cmOtherSource = value || '';
  const el = document.getElementById('cm-settings-body');
  if (el) el.innerHTML = _cmRenderMatrix();
}

async function _cmDeleteOtherProfile() {
  if (!_cmOtherSource) return;
  if (!await confirmModal({body: `Delete profile "${_cmOtherSource}"?`, danger: true})) return;
  try {
    await api('DELETE', `/api/cost-profiles/${encodeURIComponent(_cmOtherSource)}`);
    _showToast(`Deleted "${_cmOtherSource}"`, 'success');
    _cmOtherSource = '';
    await _cmLoadSettings();
  } catch(e) {
    _showToast(`Error: ${escHtml(String(e))}`, 'error');
  }
}

// _cmSaveCurrentAsProfile — captures the bot's current settings as a named
// custom profile. After save, points the Other column at the new profile so
// the operator can see it land in the matrix immediately.
async function _cmSaveCurrentAsProfile() {
  if (!_cmBot) {
    _showToast('Select a bot tab first.', 'warn');
    return;
  }
  const name = prompt('Profile name:');
  if (!name?.trim()) return;
  try {
    await api('POST', '/api/cost-profiles', { name: name.trim(), bot_id: _cmBot });
    _showToast(`✓ Saved profile "${name}"`, 'success');
    _cmOtherSource = name.trim();
    await _cmLoadSettings();
  } catch(e) {
    _showToast(`Error: ${escHtml(String(e))}`, 'error');
  }
}

// ── Section 5: Runaway Session Monitor ───────────────────────────────────────

async function _cmRefreshRunaway() {
  const el = document.getElementById('cm-runaway-body');
  if (!el) return;
  if (!_cmBot) { el.innerHTML = '<div class="empty">Select a bot above.</div>'; return; }
  el.innerHTML = '<div class="loading"><span class="spinner"></span> Scanning…</div>';
  try {
    const d = await api('GET', `/api/analytics/sessions/runaway?bot=${encodeURIComponent(_cmBot)}&days=7`);
    const data = d[_cmBot] || d;
    el.innerHTML = _cmRenderRunaway(data);
  } catch(e) {
    el.innerHTML = `<div class="empty">Unavailable: ${escHtml(String(e))}</div>`;
  }
}

function _cmRenderRunaway(d) {
  const flagged = d.flagged_sessions || [];
  const s = d.summary || {};
  const threshold = d.threshold_tokens || 100000;

  const badgeColor = flagged.length === 0 ? 'var(--green)' : 'var(--danger)';
  const header = `<div style="display:flex;align-items:center;gap:16px;margin-bottom:12px">
    <div style="font-size:0.85rem">
      <span style="color:${badgeColor};font-weight:700">${s.flagged_count ?? 0}</span>
      <span style="color:var(--text2)"> session(s) flagged</span>
    </div>
    ${s.flagged_cost > 0 ? `<div style="font-size:0.85rem;color:var(--danger)">$${(s.flagged_cost||0).toFixed(2)} in flagged session costs (${s.flagged_pct_of_total_cost ?? 0}% of total)</div>` : ''}
    <div style="margin-left:auto;font-size:0.78rem;color:var(--text3)">
      Threshold: <input type="number" id="cm-runaway-threshold" value="${threshold}" style="width:80px;font-size:0.78rem;padding:2px 6px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:4px"> tokens
      <button class="btn btn-ghost btn-sm" style="font-size:0.72rem;padding:2px 8px;margin-left:6px" onclick="_cmRefreshRunaway()">Apply</button>
    </div>
  </div>`;

  if (!flagged.length) {
    return header + `<div style="font-size:0.82rem;color:var(--green)">✓ No runaway sessions in last 7 days</div>`;
  }

  const rows = flagged.map(sess => `
    <tr>
      <td style="color:var(--text2)">${sess.date || ''}</td>
      <td style="font-family:monospace;font-size:0.75rem;color:var(--text3)">${escHtml((sess.session_id || '').slice(0,12))}…</td>
      <td style="color:var(--danger)">${((sess.total_input_tokens||0)/1000).toFixed(0)}k tok</td>
      <td>${sess.turn_count || 0}</td>
      <td style="color:var(--danger)">$${(sess.total_cost||0).toFixed(2)}</td>
      <td><span style="font-size:0.72rem;padding:2px 6px;border-radius:8px;background:var(--bg3)">${escHtml(sess.session_class||'')}</span></td>
    </tr>`).join('');

  return header + `<table style="width:100%;font-size:0.82rem;border-collapse:collapse">
    <thead><tr style="font-size:0.72rem;color:var(--text3);text-align:left">
      <th style="padding:4px 8px 8px 0">Date</th>
      <th style="padding:4px 8px 8px 0">Session</th>
      <th style="padding:4px 8px 8px 0">Ctx Size</th>
      <th style="padding:4px 8px 8px 0">Turns</th>
      <th style="padding:4px 8px 8px 0">Cost</th>
      <th style="padding:4px 8px 8px 0">Class</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>
  <div style="margin-top:10px;font-size:0.75rem;color:var(--text3)">ℹ Large context sessions are the primary driver of unexpected cost spikes. Enable compaction and context pruning to prevent them.</div>`;
}

// ── Section 6: Turn Cost Audit ────────────────────────────────────────────────

async function _cmRefreshAudit() {
  const el = document.getElementById('cm-audit-body');
  if (!el) return;
  const days = parseInt(document.getElementById('cm-audit-days')?.value || '7');
  const sort = document.getElementById('cm-audit-sort')?.value || 'ts';
  el.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';
  try {
    const botParam = _cmBot ? `&bot=${encodeURIComponent(_cmBot)}` : '&bot=all';
    const d = await api('GET', `/api/analytics/turns/audit?days=${days}&limit=25&sort=${encodeURIComponent(sort)}${botParam}`);
    el.innerHTML = _cmRenderAudit(d.turns || []);
    _respTableLabelize(el);
  } catch(e) {
    el.innerHTML = `<div class="empty">Unavailable: ${escHtml(String(e))}</div>`;
  }
}

function _cmRenderAudit(turns) {
  if (!turns.length) return '<div class="empty">No cost data for this period.</div>';

  const k = (n) => {
    n = n || 0;
    if (n === 0) return '<span style="color:var(--text3)">0</span>';
    if (n < 1000) return String(n);
    return (n/1000).toFixed(n < 10000 ? 1 : 0) + 'k';
  };

  const rows = turns.map(t => {
    const model = (t.model || '').replace('anthropic/', '');
    const cacheTotal = (t.cache_read_tokens || 0) + (t.cache_write_tokens || 0);
    const cacheCell = (t.cache_write_tokens || 0) > 0
      ? `${k(t.cache_read_tokens)} <span style="color:var(--text3);font-size:0.7rem" title="cache writes">+${k(t.cache_write_tokens)}w</span>`
      : k(t.cache_read_tokens);
    const hint = (t.source === 'cron' && t.input_tokens > 20000)
      ? '<span style="color:var(--yellow);font-size:0.7rem" title="Large cron turn — consider reducing bootstrap file size">⚠ large cron</span>'
      : '';
    const tid = t.turn_id || '';
    const clickable = tid && t.bot_id ? 'td-clickable' : '';
    const onclick = clickable
      ? ` onclick="openTurnDetail('${escHtml(tid)}','${escHtml(t.bot_id)}')" title="Open turn detail"`
      : '';
    return `<tr class="${clickable}"${onclick}>
      <td style="color:var(--text2)">${escHtml(fmtPodTime(t.ts))}</td>
      <td style="color:var(--text3)">${escHtml(t.bot_id ? botLabel(t.bot_id) : '')}</td>
      <td style="font-size:0.78rem">${escHtml(model)}</td>
      <td><span style="font-size:0.72rem;padding:2px 5px;border-radius:6px;background:var(--bg3)">${escHtml(t.source||'')}</span></td>
      <td style="color:var(--text2)">${k(t.input_tokens)}</td>
      <td style="color:var(--text2)">${cacheCell}</td>
      <td style="color:var(--text2)">${k(t.output_tokens)}</td>
      <td style="color:var(--danger);font-weight:600">${_fmtUsd(t.cost_estimated || 0)}</td>
      <td>${hint}</td>
    </tr>`;
  }).join('');

  return `<div class="resp-table-wrap"><table class="resp-table" style="width:100%;font-size:0.82rem;border-collapse:collapse">
    <thead><tr style="font-size:0.72rem;color:var(--text3);text-align:left">
      <th style="padding:4px 8px 8px 0">Time</th>
      <th style="padding:4px 8px 8px 0">Bot</th>
      <th style="padding:4px 8px 8px 0">Model</th>
      <th style="padding:4px 8px 8px 0">Source</th>
      <th style="padding:4px 8px 8px 0" title="Uncached input tokens">In</th>
      <th style="padding:4px 8px 8px 0" title="Cache reads (and writes)">Cache</th>
      <th style="padding:4px 8px 8px 0" title="Output tokens (includes thinking)">Out</th>
      <th style="padding:4px 8px 8px 0">Cost</th>
      <th style="padding:4px 8px 8px 0"></th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table></div>
  <div style="margin-top:8px;font-size:0.75rem;color:var(--text3)">ℹ Click a row for prompt, tools, and lineage. Cost = In × $3/M + Cache reads × $0.30/M + Cache writes × $3.75/M + Out × $15/M (Sonnet). Output includes extended-thinking tokens. Times in pod TZ (${escHtml(podTz())}).</div>`;
}

// ── Turn detail drawer ───────────────────────────────────────────────────────
// Backed by GET /api/analytics/turns/<turn_id>?bot=<bot_id>. Opens when the
// user clicks a Turn Audit row; shows annotation, cost_event lineage, and a
// best-effort OC transcript slice (redacted + truncated).

async function openTurnDetail(turnId, botId) {
  if (!turnId || !botId) return;
  const drawer = document.getElementById('turndetail-drawer');
  const overlay = document.getElementById('turndetail-overlay');
  const body = document.getElementById('turndetail-body');
  const title = document.getElementById('turndetail-title');
  if (!drawer || !overlay || !body) return;
  title.textContent = `Turn detail · ${botLabel(botId)}`;
  body.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';
  overlay.classList.add('open');
  drawer.classList.add('open');
  try {
    const d = await api('GET', `/api/analytics/turns/${encodeURIComponent(turnId)}?bot=${encodeURIComponent(botId)}`);
    if (!d || d.error) {
      body.innerHTML = `<div class="empty">Couldn't load turn: ${escHtml(d && d.error || 'unknown error')}</div>`;
      return;
    }
    body.innerHTML = _renderTurnDetail(d);
  } catch (e) {
    body.innerHTML = `<div class="empty">Couldn't load turn: ${escHtml(String(e))}</div>`;
  }
}

function closeTurnDetail() {
  document.getElementById('turndetail-drawer')?.classList.remove('open');
  document.getElementById('turndetail-overlay')?.classList.remove('open');
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && document.getElementById('turndetail-drawer')?.classList.contains('open')) {
    closeTurnDetail();
  }
});

function _renderTurnDetail(d) {
  const a = d.annotation || {};
  const t = d.turn_record || {};
  const events = d.cost_events || [];
  const tx = d.transcript || null;

  const k = (n) => {
    n = Number(n) || 0;
    if (n === 0) return '0';
    if (n < 1000) return String(n);
    return (n/1000).toFixed(n < 10000 ? 1 : 0) + 'k';
  };
  const cost = (a.cost_estimated != null) ? Number(a.cost_estimated) : 0;
  const triggerKinds = [...new Set(events.map(e => e.trigger_kind).filter(Boolean))];
  const cacheStates = [...new Set(events.map(e => e.cache_state).filter(Boolean))];

  // ── Header summary ─────────────────────────────────────────────────────
  const headParts = [
    `<div style="font-size:1.1rem;font-weight:600;color:var(--danger);margin-bottom:2px">${_fmtUsd(cost)}</div>`,
    `<div style="color:var(--text3);font-size:0.78rem;margin-bottom:8px">${escHtml(fmtPodTime(a.ts || ''))} · ${escHtml(a.session_class || 'unclassified')} · turn ${escHtml(String(a.resolution_turn || '?'))}</div>`,
  ];

  // ── Tokens table ───────────────────────────────────────────────────────
  const tokens = `<div class="td-section">
    <div class="td-section-title">Tokens</div>
    <table style="width:100%;font-size:0.78rem;border-collapse:collapse">
      <thead><tr style="color:var(--text3);font-size:0.7rem;text-align:left">
        <th style="padding:3px 6px 3px 0">Input</th>
        <th style="padding:3px 6px 3px 0">Cache read</th>
        <th style="padding:3px 6px 3px 0">Cache write</th>
        <th style="padding:3px 6px 3px 0">Output</th>
      </tr></thead>
      <tbody><tr>
        <td style="padding:3px 6px 3px 0">${k(a.input_tokens)}</td>
        <td style="padding:3px 6px 3px 0">${k(a.cache_read_tokens)}</td>
        <td style="padding:3px 6px 3px 0">${k(a.cache_write_tokens)}</td>
        <td style="padding:3px 6px 3px 0">${k(a.output_tokens)}</td>
      </tr></tbody>
    </table>
  </div>`;

  // ── Classification + lineage ───────────────────────────────────────────
  const lineageRows = [
    ['Model', escHtml(String(a.model_selected || ''))],
    ['Tier', escHtml(String(a.model_tier || ''))],
    ['Provider', escHtml(String(a.provider || ''))],
    ['Source', escHtml(String(t.source || a.source || ''))],
    ['Channel', escHtml(String(t.channel || ''))],
    ['Auth mode', escHtml(String(t.auth_mode || a.auth_mode || ''))],
    ['Session ID', `<span style="font-family:ui-monospace,monospace;font-size:0.72rem">${escHtml(String(a.session_id || ''))}</span>`],
    ['Turn ID', `<span style="font-family:ui-monospace,monospace;font-size:0.72rem">${escHtml(String(a.turn_id || ''))}</span>`],
    ['Class confidence', a.class_confidence != null ? Number(a.class_confidence).toFixed(2) : '—'],
    ['Correction', a.correction_detected ? '<span style="color:var(--yellow)">yes</span>' : 'no'],
  ];
  if (triggerKinds.length) lineageRows.push(['Trigger kind', triggerKinds.map(x => `<span class="td-pill">${escHtml(x)}</span>`).join('')]);
  if (cacheStates.length) lineageRows.push(['Cache state', cacheStates.map(x => `<span class="td-pill">${escHtml(x)}</span>`).join('')]);

  const lineage = `<div class="td-section">
    <div class="td-section-title">Lineage</div>
    <div class="td-kv">
      ${lineageRows.map(([k2, v]) => `<div class="k">${k2}</div><div class="v">${v}</div>`).join('')}
    </div>
  </div>`;

  // ── Cost events (Budget Hawk v2 lineage) ──────────────────────────────
  const eventsBlock = events.length
    ? `<div class="td-section">
        <div class="td-section-title">Cost events (${events.length})</div>
        <table style="width:100%;font-size:0.74rem;border-collapse:collapse">
          <thead><tr style="color:var(--text3);text-align:left">
            <th style="padding:3px 6px 3px 0">Time</th>
            <th style="padding:3px 6px 3px 0">Trigger</th>
            <th style="padding:3px 6px 3px 0">Cache</th>
            <th style="padding:3px 6px 3px 0">Cost</th>
          </tr></thead>
          <tbody>${events.map(e => `<tr>
            <td style="padding:3px 6px 3px 0;color:var(--text2)">${escHtml(fmtPodTime(e.ts || ''))}</td>
            <td style="padding:3px 6px 3px 0"><span class="td-pill">${escHtml(e.trigger_kind || '—')}</span></td>
            <td style="padding:3px 6px 3px 0"><span class="td-pill">${escHtml(e.cache_state || '—')}</span></td>
            <td style="padding:3px 6px 3px 0;color:var(--text2)">${_fmtUsd(e.cost_usd || 0)}</td>
          </tr>`).join('')}</tbody>
        </table>
      </div>`
    : `<div class="td-section">
        <div class="td-section-title">Cost events</div>
        <div style="color:var(--text3);font-size:0.78rem">No cost_event records joined for this turn.</div>
      </div>`;

  // ── Transcript ─────────────────────────────────────────────────────────
  let transcriptBlock;
  if (!tx) {
    const reason = d.transcript_status || 'not_found';
    transcriptBlock = `<div class="td-section">
      <div class="td-section-title">Transcript</div>
      <div style="color:var(--text3);font-size:0.78rem">Transcript not available (${escHtml(reason)}). The turn_annotation and cost_event records above carry everything we have for this turn.</div>
    </div>`;
  } else {
    const sectionHtml = (label, sec) => {
      if (!sec || !sec.chars) return '';
      const trunc = sec.truncated ? ' <span style="color:var(--yellow);font-size:0.7rem">(truncated)</span>' : '';
      return `<div style="margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:3px">
          <span style="font-size:0.74rem;font-weight:600;color:var(--text2)">${label}</span>
          <span style="font-size:0.7rem;color:var(--text3)">${sec.chars.toLocaleString()} chars${trunc}</span>
        </div>
        <pre class="td-pre td-collapsed" onclick="this.classList.toggle('td-collapsed')">${escHtml(sec.text || '')}</pre>
        <button class="td-toggle" onclick="this.previousElementSibling.classList.toggle('td-collapsed')">expand / collapse</button>
      </div>`;
    };
    const toolList = (tx.tool_calls || []).map(tc => `
      <div class="td-tool-row">
        <div class="td-tool-name">${escHtml(tc.tool || 'unknown')}</div>
        <div class="td-tool-chars" title="input chars">in ${tc.input_chars.toLocaleString()}</div>
        <div class="td-tool-chars" title="result chars">out ${tc.result_chars.toLocaleString()}</div>
      </div>`).join('');
    const summaryEntries = Object.entries(tx.tool_summary || {});
    const summaryPills = summaryEntries.length
      ? summaryEntries.map(([n, c]) => `<span class="td-pill">${escHtml(n)} × ${c}</span>`).join(' ')
      : '<span style="color:var(--text3);font-size:0.78rem">No tool calls in this turn.</span>';

    transcriptBlock = `<div class="td-section">
      <div class="td-section-title">Transcript</div>
      ${d.transcript_source ? `<div style="font-size:0.7rem;color:var(--text3);margin-bottom:6px">Source: <span style="font-family:ui-monospace,monospace">${escHtml(d.transcript_source)}</span></div>` : ''}
      ${sectionHtml('System prompt', tx.system)}
      ${sectionHtml('User message', tx.user)}
      ${sectionHtml('Assistant message', tx.assistant)}
      <div style="margin-top:8px">
        <div style="font-size:0.74rem;font-weight:600;color:var(--text2);margin-bottom:4px">Tool calls (${tx.tools_invoked || 0})</div>
        <div style="margin-bottom:6px">${summaryPills}</div>
        ${toolList || ''}
      </div>
    </div>`;
  }

  return headParts.join('') + tokens + lineage + eventsBlock + transcriptBlock;
}

// ── Section 7: Spike Explorer (Budget Hawk v2) ───────────────────────────────

async function _cmRefreshSpikes() {
  const body = document.getElementById('cm-spikes-body');
  const rollupEl = document.getElementById('cm-spikes-rollup');
  const appRollupEl = document.getElementById('cm-spikes-app-rollup');
  if (!body) return;
  const days = parseInt(document.getElementById('cm-spike-days')?.value || '7');
  body.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';
  if (rollupEl) rollupEl.innerHTML = '';
  if (appRollupEl) appRollupEl.innerHTML = '';
  try {
    const botParam = _cmBot ? `&bot=${encodeURIComponent(_cmBot)}` : '&bot=all';
    const [spikesResp, rollupResp, appResp] = await Promise.all([
      api('GET', `/api/cost-measures/forensics/spikes?days=${days}&limit=20${botParam}`),
      api('GET', `/api/cost-measures/forensics/trigger-rollup?days=${days}${botParam}`),
      api('GET', `/api/cost-measures/forensics/app-rollup?days=${days}${botParam}`),
    ]);
    if (rollupEl) rollupEl.innerHTML = _cmRenderTriggerRollup(rollupResp.combined || {});
    if (appRollupEl) appRollupEl.innerHTML = _cmRenderAppRollup(appResp.combined || {});
    body.innerHTML = _cmRenderSpikes(spikesResp.spikes || [], spikesResp.total_events_scanned || 0);
  } catch(e) {
    body.innerHTML = `<div class="empty">Unavailable: ${escHtml(String(e))}</div>`;
  }
}

function _cmRenderAppRollup(rollup) {
  // Same visual shape as the trigger_kind strip so operators can scan
  // "where is my money going, by topic?" in one glance. Always shows
  // "(unattributed)" when present so gaps in keyword coverage are visible.
  const entries = Object.entries(rollup || {});
  if (!entries.length) return '';
  entries.sort((a, b) => (b[1].cost_usd || 0) - (a[1].cost_usd || 0));
  const total = entries.reduce((s, [, v]) => s + (v.cost_usd || 0), 0);
  if (total <= 0) return '';
  const parts = entries.slice(0, 10).map(([app, v]) => {
    const pct = total > 0 ? Math.round((v.cost_usd || 0) / total * 100) : 0;
    const isUnattr = app === '(unattributed)';
    const nameStyle = isUnattr ? 'color:var(--yellow);font-style:italic' : 'color:var(--text3)';
    return `<span style="display:inline-block;margin-right:14px;font-size:0.78rem">
      <span style="${nameStyle}">${escHtml(app)}</span>
      <span style="color:var(--text);font-weight:600;margin-left:4px">${_fmtUsd(v.cost_usd || 0)}</span>
      <span style="color:var(--text3);margin-left:4px">(${pct}%)</span>
      <span style="color:var(--text3);margin-left:4px">·</span>
      <span style="color:var(--text3);margin-left:4px">${v.session_count||0} sess</span>
    </span>`;
  }).join('');
  const overflow = entries.length > 10
    ? `<span style="color:var(--text3);font-size:0.72rem">+${entries.length - 10} more</span>`
    : '';
  return `<div style="padding:8px 10px;background:var(--bg3);border-radius:6px;border:1px solid var(--border)">
    <div style="font-size:0.7rem;color:var(--text3);margin-bottom:4px">
      Cost by application
      <span style="color:var(--text3);margin-left:6px" title="Attribution: sessions' cost is split evenly across their detected applications_invoked tags. Extend patterns in network.json applicationPatterns to shrink (unattributed).">ℹ</span>
    </div>
    ${parts}
    ${overflow}
  </div>`;
}

function _cmRenderTriggerRollup(rollup) {
  // Flat horizontal strip: "user_turn $2.31 · heartbeat $0.89 · summarizer $0.14 · …"
  // Keeps the focus on cost attribution in a single glance.
  const entries = Object.entries(rollup || {});
  if (!entries.length) return '';
  entries.sort((a, b) => (b[1].cost_usd || 0) - (a[1].cost_usd || 0));
  const total = entries.reduce((s, [, v]) => s + (v.cost_usd || 0), 0);
  if (total <= 0) return '';
  const parts = entries.map(([kind, v]) => {
    const pct = total > 0 ? Math.round((v.cost_usd || 0) / total * 100) : 0;
    return `<span style="display:inline-block;margin-right:14px;font-size:0.78rem">
      <span style="color:var(--text3)">${escHtml(kind)}</span>
      <span style="color:var(--text);font-weight:600;margin-left:4px">${_fmtUsd(v.cost_usd || 0)}</span>
      <span style="color:var(--text3);margin-left:4px">(${pct}%)</span>
      <span style="color:var(--text3);margin-left:4px">·</span>
      <span style="color:var(--text3);margin-left:4px">${v.event_count||0} calls</span>
    </span>`;
  }).join('');
  return `<div style="padding:8px 10px;background:var(--bg3);border-radius:6px;border:1px solid var(--border)">
    <div style="font-size:0.7rem;color:var(--text3);margin-bottom:4px">Cost by trigger_kind</div>
    ${parts}
  </div>`;
}

function _cmRenderSpikes(spikes, totalScanned) {
  if (!spikes.length) {
    return '<div class="empty">No cost events recorded yet. The cost ledger starts populating after the plugin is updated and a few LLM calls have occurred.</div>';
  }

  // Colour cache_state so "invalidated" (the post-key-rotation fingerprint)
  // jumps out of the table.
  const cacheBadge = (state) => {
    const colors = {
      warm:        { bg: 'rgba(34,197,94,0.15)',   fg: 'var(--green)'  },
      fresh:       { bg: 'rgba(99,102,241,0.15)',  fg: '#a5b4fc'       },
      invalidated: { bg: 'rgba(239,68,68,0.15)',   fg: 'var(--danger)' },
      unknown:     { bg: 'var(--bg3)',              fg: 'var(--text3)'  },
    };
    const c = colors[state] || colors.unknown;
    return `<span style="font-size:0.7rem;padding:2px 6px;border-radius:6px;background:${c.bg};color:${c.fg}">${escHtml(state || 'unknown')}</span>`;
  };

  const rows = spikes.map(e => {
    const model = (e.model || '').replace('anthropic/', '');
    const sid = (e.session_id || '').slice(0, 8);
    const tokens = (e.input_tokens || 0) + (e.cache_read_tokens || 0) + (e.cache_write_tokens || 0);
    return `<tr>
      <td style="color:var(--text2);white-space:nowrap">${(e.ts||'').slice(5,16)}</td>
      <td style="color:var(--text3)">${escHtml(botLabel(e.bot_id))}</td>
      <td><span style="font-size:0.72rem;padding:2px 6px;border-radius:6px;background:var(--bg3);color:var(--text2)">${escHtml(e.trigger_kind || 'unknown')}</span></td>
      <td style="font-size:0.78rem">${escHtml(model)}</td>
      <td>${cacheBadge(e.cache_state)}</td>
      <td style="color:var(--text3);font-size:0.72rem">${sid || '—'}</td>
      <td style="color:var(--text2)">${(tokens/1000).toFixed(1)}k</td>
      <td style="color:var(--danger);font-weight:600">${_fmtUsd(e.cost_usd || 0)}</td>
    </tr>`;
  }).join('');

  return `<table style="width:100%;font-size:0.82rem;border-collapse:collapse">
    <thead><tr style="font-size:0.72rem;color:var(--text3);text-align:left">
      <th style="padding:4px 8px 8px 0">Time</th>
      <th style="padding:4px 8px 8px 0">Bot</th>
      <th style="padding:4px 8px 8px 0">Trigger</th>
      <th style="padding:4px 8px 8px 0">Model</th>
      <th style="padding:4px 8px 8px 0">Cache</th>
      <th style="padding:4px 8px 8px 0">Session</th>
      <th style="padding:4px 8px 8px 0">Tokens</th>
      <th style="padding:4px 8px 8px 0">Cost</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>
  <div style="margin-top:8px;font-size:0.72rem;color:var(--text3)">ℹ Scanned ${totalScanned} cost events. <b>invalidated</b> cache state = prompt re-written from scratch (post-key-rotation / config churn); <b>warm</b> = cache hit (typical multi-turn session).</div>`;
}

// ── Utility: toast notification (if not already defined) ──────────────────────
if (typeof _showToast !== 'function') {
  function _showToast(msg, type = 'info') {
    const colors = { success: 'var(--green)', error: 'var(--danger)', warn: 'var(--yellow)', info: 'var(--text)' };
    const el = document.createElement('div');
    el.style.cssText = `position:fixed;bottom:24px;right:24px;padding:10px 16px;border-radius:8px;background:var(--bg3);border:1px solid var(--border);color:${colors[type]||colors.info};font-size:0.85rem;z-index:9999;box-shadow:0 4px 16px rgba(0,0,0,0.4);max-width:380px`;
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 3500);
  }
}

// ══════════════════════════════════════════════════════
// ══════════════════════════════════════════════════════
// Cost Import Modal
// ══════════════════════════════════════════════════════
function openCostImportModal() {
  document.getElementById('cost-import-data').value = '';
  document.getElementById('cost-import-result').textContent = '';
  document.getElementById('cost-import-modal').classList.add('active');
}
function closeCostImportModal() {
  document.getElementById('cost-import-modal').classList.remove('active');
}
async function submitCostImport() {
  const raw = document.getElementById('cost-import-data').value.trim();
  const fmt = document.getElementById('cost-import-fmt').value;
  const resultEl = document.getElementById('cost-import-result');
  if (!raw) { resultEl.innerHTML = '<span style="color:var(--danger)">Paste some data first.</span>'; return; }
  resultEl.innerHTML = '<span style="color:var(--text2)">Importing…</span>';
  const r = await api('POST', '/api/cost/import', { data: raw, format: fmt });
  if (r.error) {
    resultEl.innerHTML = `<span style="color:var(--danger)">✗ ${escHtml(r.error)}</span>`;
  } else {
    const rows = r.rows_imported ?? r.rows ?? '?';
    const dates = r.dates_updated ?? [];
    resultEl.innerHTML = `<span style="color:var(--green)">✓ Imported ${rows} record${rows!==1?'s':''}${dates.length ? ' across ' + dates.length + ' date(s)' : ''}. Refresh Cost page to see updated data.</span>`;
    toast('Cost data imported', 'ok');
    setTimeout(closeCostImportModal, 2500);
  }
}


// ════════════════════════════════════════════════════════════════════════
// Page: Overview — per-tile action menu + renderOverview + renderPodNode
//                  + bot lifecycle actions
//
// The Overview page's rendering + per-tile interaction surfaces. The
// loadStatus() data fetcher itself remains inline in the main script
// because it's also called from a dozen post-action refresh callbacks
// across other extracted modules (see comment below).
//
// Cluster contents:
//
//   Per-tile action menu (⋯):
//     _podMenuCloseAll + _podMenuToggle + _podMenuRun
//     _podMenuAppScan + _podMenuSecurityCheck + _podMenuBackup
//     _podMenuViewBackups + _podMenuOpenBotConfig
//     _podTileMenuHtml (builds the menu DOM for one tile)
//
//   Overview render + Release & update drawer:
//     renderOverview + renderPodNode (the main tile renderer)
//     renderUpdatesDrawer + _drawerCanarySection + _drawerDirectSection
//     + snoozeUpgrade + renderFunnel
//
//   Tile window + chip nav + chip explainer:
//     setTileWindow + chipNav + openChipExplainer + closeChipExplainer
//     _chipExplainerAction + chipScroll + setUsageUnit
//
//   Bot lifecycle actions (Overview tile actions):
//     redeployBot + _lifecycleHandle + detachBot + retireBot +
//     confirmDeleteBot + closeDeleteBotModal + _doDeleteBotFromModal +
//     _doDeleteBot + toggleBotMultiUser + toggleBotContinuityEngine +
//     restartGatewayFromOverview
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - api(), toast(), escHtml(), botLabel(), orderedBotIds() — core/
//     + main script (botLabel/orderedBotIds in pages/bot-detail.js
//     Phase 3ac)
//   - loadStatus() — still inline in main script (refreshes after every
//     lifecycle action; called by Overview render itself + dozens of
//     post-action callbacks from other extracted pages)
//   - _statusData — still inline in main script
//   - nav() / window.nav — core/router.js
//   - _betterTypeLabel — still inline
//   - openSetupChecklistModal — pages/bot-detail.js (Phase 3ac)
//   - openProposalDetail — pages/self-improvement.js
// ════════════════════════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════
// Per-tile action menu (⋯) — open/close + click-away
// ══════════════════════════════════════════════════════
// Replaces the old inline button row on each pod tile. One menu open
// at a time; clicking the ⋯ on another tile auto-closes the previous
// popover. The doc-level click handler dismisses on any click outside
// a `.pod-tile-menu` wrapper.

function _podMenuCloseAll() {
  document.querySelectorAll('.pod-tile-menu-pop').forEach(el => { el.style.display = 'none'; });
}

function _podMenuToggle(botId) {
  const target = document.getElementById(`pod-tile-menu-${botId}`);
  if (!target) {
    // The button exists (we got called from it) but the popover for
    // this bot isn't in the DOM. This is the "Actions ⋯ inert"
    // failure mode reported across SW updates. Log a concrete
    // diagnostic and tell the operator how to recover. Reproducing
    // remained elusive, so the visible toast is part of the
    // diagnostic loop — if it ever fires, the user can capture it.
    console.warn('[podMenu] popover not found for bot', botId,
      '— DOM is stale; run evolveMenuDiag() and refresh the app to recover');
    if (typeof toast === 'function') {
      toast('Menu state stale — relaunch the app to fix', 'err');
    }
    return;
  }
  const wasOpen = target.style.display === 'block';
  _podMenuCloseAll();
  if (!wasOpen) target.style.display = 'block';
}

// Catch-all for script-load errors so the "Actions inert after deploy"
// failure mode leaves a clean trail. If the main script halts mid-init
// (e.g. a SyntaxError in a later block) the buttons render but no
// handler is wired; this listener attaches early enough — registered
// on `window` at script-execution time — to log the originating error.
window.addEventListener('error', (ev) => {
  if (ev && ev.error) {
    console.error('[evolve] uncaught script error', ev.error, 'at', ev.filename + ':' + ev.lineno);
  }
});

// Operator-facing diagnostic: paste `evolveMenuDiag()` into the
// browser console when the Actions ⋯ button isn't responding and
// share the output. It captures everything we'd want to know to
// pinpoint the failure mode — whether the helper functions are
// defined, whether the buttons/popovers are in the DOM, whether
// their IDs line up, and which SW is controlling the page.
window.evolveMenuDiag = function evolveMenuDiag() {
  const buttons = Array.from(document.querySelectorAll('button[data-pod-menu-id]'));
  const popovers = Array.from(document.querySelectorAll('.pod-tile-menu-pop'));
  const buttonIds = buttons.map(b => b.dataset.podMenuId);
  const popoverIds = popovers.map(p => p.id.replace(/^pod-tile-menu-/, ''));
  const missing = buttonIds.filter(id => !popoverIds.includes(id));
  const info = {
    when: new Date().toISOString(),
    helpersDefined: {
      _podMenuToggle: typeof _podMenuToggle === 'function',
      _podMenuCloseAll: typeof _podMenuCloseAll === 'function',
      _podMenuRun: typeof _podMenuRun === 'function',
    },
    buttonCount: buttons.length,
    popoverCount: popovers.length,
    buttonIds,
    popoverIds,
    botsWithMissingPopover: missing,
    sw: {
      controller: navigator.serviceWorker?.controller?.scriptURL || 'none',
      ready: !!navigator.serviceWorker?.ready,
    },
    documentReadyState: document.readyState,
    visibilityState: document.visibilityState,
  };
  console.log('[evolveMenuDiag]', info);
  return info;
};

// Menu items wrap their action through _podMenuRun so the popover
// closes as soon as the action fires — operators never have to click
// the body to dismiss after picking something.
function _podMenuRun(fn) {
  _podMenuCloseAll();
  try { fn(); } catch (e) { console.error('Tile menu action failed', e); }
}

// Delegated handler: covers BOTH the Actions ⋯ button (open/close)
// AND the outside-click dismissal. Using a single doc-level listener
// avoids relying on per-button inline onclick attributes, which have
// proven brittle after SW-driven updates — the symptom was "Actions
// button inert after a new Evolve release", surviving a reload but
// fixed by closing/reopening the PWA. Delegation is wired exactly
// once at script load, so as long as the script reaches this line
// (which we can prove via DOM ready), every tile rendered later
// — including tiles re-rendered after status polls — gets working
// buttons without per-render wiring.
document.addEventListener('click', (ev) => {
  if (!ev.target || !ev.target.closest) return;
  // Open/close the menu when the operator clicks the ⋯ Actions button.
  // The button declares its bot id in data-pod-menu-id so we don't
  // have to embed it in JS-as-attribute.
  const toggleBtn = ev.target.closest('button[data-pod-menu-id]');
  if (toggleBtn) {
    ev.stopPropagation();
    try {
      _podMenuToggle(toggleBtn.dataset.podMenuId);
    } catch (e) {
      // Surfaces clean evidence when the recurring "inert button"
      // failure happens — most plausible causes (undefined helper
      // after a partial script load, popover element pruned by a
      // late mutation) all throw or no-op here, never silently.
      console.error('[podMenu] toggle failed for', toggleBtn.dataset.podMenuId, e);
      if (typeof toast === 'function') {
        toast('Menu glitch — relaunch the app to fix', 'err');
      }
    }
    return;
  }
  // Clicks inside an open menu (a menu item, the wrapper, etc.) don't
  // close it from here — menu items close themselves via _podMenuRun
  // after their action fires.
  const inside = ev.target.closest('.pod-tile-menu') || ev.target.closest('.pod-tile-menu-pop');
  if (!inside) _podMenuCloseAll();
}, true);

// New action wrappers — wired to existing per-bot endpoints. Each one
// toasts on launch; long-running work continues server-side and the
// operator can drill into the bot's detail surface to watch progress.

// Deprecated alias — the tile menu now invokes runSync() (the unified
// "Sync apps" action defined in pages/apps.js) directly. Kept as a thin
// delegate so any lingering caller keeps working; falls back to the raw
// scan endpoint if apps.js hasn't loaded for some reason.
async function _podMenuAppScan(botId) {
  if (typeof runSync === 'function') { runSync(botId); return; }
  toast(`App scan started for ${botLabel(botId)}…`, 'ok');
  const r = await api('POST', `/api/applications/scan?bot=${encodeURIComponent(botId)}`);
  if (r?.error) { toast(`✗ ${r.error}`, 'err'); return; }
  if (r?.status === 'already_running') { toast(`Scan already running for ${botLabel(botId)}`, 'warn'); return; }
  toast(`✓ Scan running — open Apps to watch progress`, 'ok');
}

async function _podMenuSecurityCheck(botId) {
  toast(`Security audit started for ${botLabel(botId)}…`, 'ok');
  const r = await api('POST', `/api/security/audit/refresh?bot=${encodeURIComponent(botId)}`);
  if (r?.error) { toast(`✗ ${r.error}`, 'err'); return; }
  toast(`✓ Audit running — open Security to see results`, 'ok');
}

async function _podMenuBackup(botId) {
  toast(`Backup started for ${botLabel(botId)}…`, 'ok');
  const r = await api('POST', '/api/backup/cloud/run', { botId });
  if (!r?.ok) { toast(`✗ Backup failed: ${r?.error || 'unknown error'}`, 'err'); return; }
  const status = (r.results && r.results[0] && r.results[0].status) || 'ok';
  const ok = status === 'ok' || status === 'no-changes';
  toast(`Backup ${ok ? 'complete' : status} for ${botLabel(botId)}`, ok ? 'ok' : 'warn');
}

function _podMenuViewBackups() {
  const link = document.querySelector('[data-page=backup]');
  if (link) nav(link);
}

// Deep-link from a Dashboard / Overview tile (or a chip popover
// remediation) to the per-bot config view. The Customizations card,
// the Daily Spend Cap, the Self-Healing per-bot overrides, etc. all
// live on Settings → Bots → <bot tab>. After the 2026-06-01
// restructure that's two subtab activations + a bot switch (down from
// three levels + a bot switch in the prior nesting), but we still
// force them explicitly here so the path is independent of which
// Settings subtab the operator last visited.
function _podMenuOpenBotConfig(botId) {
  const navEl = document.querySelector('[data-page=settings]');
  if (!navEl) {
    toast('Settings page not found', 'err');
    return;
  }
  nav(navEl);
  const botsTab = document.querySelector('#page-settings .subtab[data-subtab="bots"]');
  if (botsTab) subTab(botsTab, 'settings', 'bots');
  // initConfigBotSelector renders the per-bot tab strip on first paint
  // for this page; on subsequent loads switchConfigBot() flips the
  // selection without a re-init. Defer the bot switch one tick so the
  // tab strip is rendered first.
  setTimeout(() => {
    if (typeof switchConfigBot === 'function') {
      try { switchConfigBot(botId); } catch (e) { console.error('switchConfigBot failed', e); }
    }
    // Scroll the per-bot tab strip into view — the topmost stable anchor
    // for bot-specific settings. Anchoring on the tab strip (rather than
    // any one card below it) means the deep-link lands the same place
    // even if cards are reordered or added below in future work.
    const topAnchor = document.getElementById('config-bot-tabs');
    if (topAnchor && topAnchor.scrollIntoView) {
      topAnchor.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, 50);
}

// Builds the menu DOM for one tile. Kept separate from renderPodNode
// so the action surface can be re-skinned without touching the
// status-scan rendering.
function _podTileMenuHtml(id, b, isPrimary) {
  const ceEnabled = b.continuity_engine_enabled !== false;
  const ceStateLabel = ceEnabled ? 'On' : 'Off';
  // Three first-class removal paths, mirroring the CLI lifecycle commands.
  // Single "Remove Bot" used to call the broken /api/remove (only deleted
  // a legacy plist + network entry while leaving 7+ daemons running) —
  // see retire.delete_bot docstring + project memory for the full story.
  const removalSection = isPrimary ? '' : `
        <div class="pod-tile-menu-divider"></div>
        <div class="pod-tile-menu-section">Removal</div>
        <button class="pod-tile-menu-item"
                title="Stop Evolve observing this bot. The bot keeps running as an OpenClaw bot. Reversible: a re-deploy puts the plugin back."
                onclick="_podMenuRun(()=>detachBot('${id}'))">🔌 Detach from Evolve</button>
        <button class="pod-tile-menu-item"
                title="Archive bot data + stop all daemons + remove from network. macOS user account stays. Reversible from the archive."
                onclick="_podMenuRun(()=>retireBot('${id}'))">📦 Retire Bot…</button>
        <button class="pod-tile-menu-item pod-tile-menu-item-danger"
                title="IRREVERSIBLE: retire + delete the macOS user account + /Users/${id}/. Requires typing DELETE."
                onclick="_podMenuRun(()=>confirmDeleteBot('${id}'))">🗑 Delete Bot…</button>`;
  const label = escHtml(botLabel(id));
  // Setup checklist menu item — escape hatch when the operator suppressed
  // the on-tile Setup chip but still has pending items. Computed
  // server-side via setup_checklist.should_show_in_actions_menu so the
  // menu stays clean for bots whose chip is already visible (the chip
  // is the entry point in that case) or whose checklist is fully done.
  const setupInMenu = !!(b.tile && b.tile.setup_in_actions_menu);
  const setupMenuItem = setupInMenu
    ? `<button class="pod-tile-menu-item"
               title="Pick up where you left off with this bot's setup checklist"
               onclick="_podMenuRun(()=>openSetupChecklistModal('${id}'))">📋 Setup checklist</button>`
    : '';
  return `
    <div class="pod-tile-menu">
      <button class="btn btn-ghost btn-sm" data-pod-menu-id="${id}"
              title="Actions for ${label}">Actions ⋯</button>
      <div id="pod-tile-menu-${id}" class="pod-tile-menu-pop" role="menu">
        <div class="pod-tile-menu-section">Lifecycle</div>
        <button class="pod-tile-menu-item" onclick="_podMenuRun(()=>redeployBot('${id}','${b.role}'))">↻ Redeploy</button>
        <button class="pod-tile-menu-item" onclick="_podMenuRun(()=>restartGatewayFromOverview('${id}'))">⟳ Restart</button>
        <div class="pod-tile-menu-divider"></div>
        <div class="pod-tile-menu-section">Inspect</div>
        <button class="pod-tile-menu-item" title="Run the verification gauntlet: file ownership, agent config, channel handshake." onclick="_podMenuRun(()=>_verifyModalOpen('${id}'))">🔍 Verify Setup</button>
        <button class="pod-tile-menu-item" title="Discover new apps and audit manifest provenance in one pass" onclick="_podMenuRun(()=>runSync('${id}'))">🔄 Sync apps</button>
        <button class="pod-tile-menu-item" onclick="_podMenuRun(()=>_podMenuSecurityCheck('${id}'))">🛡 Run Security Check</button>
        <div class="pod-tile-menu-divider"></div>
        <div class="pod-tile-menu-section">Backup</div>
        <button class="pod-tile-menu-item" onclick="_podMenuRun(()=>_podMenuBackup('${id}'))">💾 Manual Backup</button>
        <button class="pod-tile-menu-item" onclick="_podMenuRun(()=>_podMenuViewBackups())">📁 View Backups</button>
        <div class="pod-tile-menu-divider"></div>
        <div class="pod-tile-menu-section">Configure</div>
        ${setupMenuItem}
        <button class="pod-tile-menu-item" title="Open Settings → Bots for ${label} (cacheRetention, session budget cap, models, caps, etc.)" onclick="_podMenuRun(()=>_podMenuOpenBotConfig('${id}'))">⚙ Settings</button>
        <button class="pod-tile-menu-item" onclick="_podMenuRun(()=>toggleBotContinuityEngine('${id}', ${ceEnabled}))">⚙ Continuity Engine <span class="pod-tile-menu-item-state">${ceStateLabel}</span></button>
        <button class="pod-tile-menu-item" onclick="_podMenuRun(()=>openBreakerModal('${id}', false))">⚡ Circuit Breakers</button>${removalSection}
      </div>
    </div>`;
}

// ══════════════════════════════════════════════════════
// Overview
// ══════════════════════════════════════════════════════
function renderOverview() {
  if (!_statusData) return;
  const bots = _statusData.bots || {};
  // Sync the window-toggle button state to the persisted choice (no-op on
  // re-renders since we update the buttons inside setTileWindow too).
  document.querySelectorAll('#pod-window-toggle button').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.window === (window._tileWindow || '7d'));
  });
  // Derive online status. The heal-written status file is authoritative when
  // fresh (gateway_status_fresh=true): if the gateway just probed unreachable,
  // the bot is offline regardless of how recent the metrics file is — we
  // shouldn't count a downed bot as "online" just because it wrote metrics
  // before the gateway crashed.
  function botGatewayDown(b) {
    return b.gateway_status_fresh === true && b.gateway_reachable === false;
  }
  function botOnline(b) {
    if (botGatewayDown(b)) return false;
    return b.live === true || (b.last_metric_date != null);
  }
  function botStatusLabel(b) {
    if (botGatewayDown(b)) return 'offline';
    if (b.live) return 'online';
    if (b.last_metric_date) return 'active';
    return 'offline';
  }
  function botStatusBadge(b) {
    const s = botStatusLabel(b);
    return badge(s, s === 'online' ? 'ok' : s === 'active' ? 'warn' : s === 'not installed' ? 'member' : 'crit');
  }
  const allBots = Object.values(bots);
  const online = allBots.filter(b => botOnline(b)).length;
  document.getElementById('ov-bots').textContent = `${online}/${allBots.length}`;

  // Pod node rendering
  const el = document.getElementById('overview-bots');
  // Primary bot first, then others alphabetised by label — see orderedBotIds().
  const orderedEntries = orderedBotIds(bots).map(id => [id, bots[id]]);
  // Activity bars are now 100%-width composition graphs (human / scheduled
  // / background). Volume comparison across tiles happens by reading the
  // numbers themselves rather than via bar width — clarity of composition
  // beats relative-volume scaling for an at-a-glance read.
  el.innerHTML = orderedEntries.map(([id, b]) => renderPodNode(id, b)).join('') || '<div class="empty" style="grid-column:1/-1">No pods configured.</div>';

  // Consolidated "Release & update" drawer — one card merging the canary
  // release pipeline, the Evolve update/soak state, and the OpenClaw runtime
  // upgrade into stacked rows (was three separate boxes). Synchronous render
  // from the cached snapshot; it self-refreshes the git-backed status when
  // stale. See docs/spec-release-tiers-2026-05-16.md + spec-deploy-meta §D-8.
  renderUpdatesDrawer();

  // Consolidated UPDATES summary cell + slim loud banner (folds the lag
  // banner, release panel, and OC banner into one above-the-fold token).
  _refreshUpdatesCell();
}

// Renders a single bot's `pod-node` tile — the operator-grade card with
// status ring, identity pills, activity/cost/apps blocks, health chips,
// and Redeploy/Restart/Remove. Used by the Overview page AND the Home
// page's right-rail, so both surfaces stay in lockstep without a
// drift-prone duplicate. Globals it reads: `_statusData` (for the
// current Evolve version + member counts in pills), `_tileWindow`
// (selected metric window).
function renderPodNode(id, b) {
  function botGatewayDown(b) {
    return b.gateway_status_fresh === true && b.gateway_reachable === false;
  }
  function botStatusLabel(b) {
    if (botGatewayDown(b)) return 'offline';
    if (b.live) return 'online';
    if (b.last_metric_date) return 'active';
    return 'offline';
  }
  {
    const isPrimary = b.role === 'primary';
    const status = botStatusLabel(b);
    // Circuit-breaker state — hoisted to the top of the function so
    // every render decision below (ring color, Healthy chip, action
    // row, pill) consults one source of truth. Tile_metrics doesn't
    // know about breakers, so a freshly-tripped L2 bot still reports
    // gateway_running=true from cached probe data; the breaker state
    // overrides that here.
    const _breakerTypes = new Set((b.active_breakers || []).map(x => x.type));
    const _podBreakerTypes = new Set((_statusData?.pod_breakers || []).map(x => x.type));
    const _anyBreaker = _breakerTypes.size > 0 || _podBreakerTypes.size > 0;
    const _l2Tripped = _breakerTypes.has('full') || _podBreakerTypes.has('full');
    // L2 trip = bot is intentionally offline. Override the ring to red
    // regardless of stale "online" probe data.
    const ringClass = _l2Tripped ? 'pod-ring-offline'
      : status === 'online' ? 'pod-ring-online'
      : status === 'active' ? 'pod-ring-active'
      : 'pod-ring-offline';
    const rawModel = b.oc_model || b.model;
    const modelTag = rawModel
      ? `<span class="pod-model-tag">${escHtml(rawModel)}</span>` : '';
    // Tile metric blocks: activity / cost / apps / health chips
    const win = window._tileWindow || '7d';
    const winLabel = win;
    const tile = b.tile || null;
    const trendArrow = (cur, prior) => {
      if (prior == null || prior === 0) {
        return cur > 0 ? `<span class="pod-trend-up">▲ new</span>` : '';
      }
      const pct = Math.round(((cur - prior) / prior) * 100);
      if (Math.abs(pct) < 5) return `<span class="pod-trend-flat">≈</span>`;
      return pct > 0
        ? `<span class="pod-trend-up">▲ ${pct}%</span>`
        : `<span class="pod-trend-down">▼ ${Math.abs(pct)}%</span>`;
    };
    const activityHtml = (() => {
      if (!tile) return '';
      const a = tile.activity;
      const turnsCur = a[`turns_${win}`];
      const turnsPrior = a[`turns_prior_${win}`];
      const sessCur = a[`sessions_${win}`];
      const sessPrior = a[`sessions_prior_${win}`];
      const humanPct = a[`human_pct_${win}`];
      if (turnsCur === 0 && sessCur === 0) {
        return `<div class="pod-activity-empty">no activity (${winLabel})</div>`;
      }
      // Two-bucket bar: human (saturated) vs auto (everything else: heartbeat,
      // cron_app, subagent, summarizer, classifier, fallback, unknown). The
      // full three-bucket split lives on tile.activity.{human,scheduled,
      // background}_pct_* so a future Usage-tab detail view can render the
      // finer granularity. For at-a-glance reading on the overview tile,
      // the bot/auto distinction is what matters; the third bucket
      // ("background") was usually 0% on real bots and added noise.
      const haveSplit = humanPct != null;
      const h = haveSplit ? Math.round(humanPct * 100) : 0;
      const auto = haveSplit ? 100 - h : 0;
      const tooltipLabel = haveSplit
        ? `${h}% human · ${auto}% automation`
        : 'composition unknown';
      const inlineLabel = haveSplit
        ? ` <span class="pod-activity-num-sub-inline">(${h}% human)</span>`
        : '';
      return `<div class="pod-activity">
        <div class="pod-activity-row">
          <span class="pod-activity-num">${turnsCur.toLocaleString()} turns${inlineLabel}</span>
          ${trendArrow(turnsCur, turnsPrior)}
        </div>
        <div class="pod-bar" title="${escHtml(tooltipLabel)}">
          ${haveSplit ? `<span class="pod-bar-human" style="width:${h}%"></span>
          <span class="pod-bar-auto" style="width:${auto}%"></span>` : ''}
        </div>
        <div class="pod-activity-row pod-activity-row-sub">
          <span class="pod-activity-num-sub">${sessCur.toLocaleString()} sessions</span>
          ${trendArrow(sessCur, sessPrior)}
        </div>
      </div>`;
    })();
    const costHtml = (() => {
      if (!tile) return '';
      const c = tile.cost;
      const cur = c[`usd_${win}`];
      const prior = c[`usd_prior_${win}`];
      if (cur === 0 && prior === 0) return '';
      return `<div class="pod-cost">
        <span class="pod-cost-num">$${cur.toFixed(2)}</span>
        <span class="pod-cost-window">(${winLabel})</span>
        ${trendArrow(cur, prior)}
      </div>`;
    })();
    const appsChip = (() => {
      if (!tile || tile.apps.total === 0) return '';
      const used = tile.apps[`used_${win}`];
      const total = tile.apps.total;
      // The "X/Y apps" pill was a static stat with a tooltip-only
      // affordance — operators reported it as inscrutable ("what does
      // 0/4 mean?"). Per docs/principle-alerts-explain-and-remediate.md
      // we synthesize an explainer object client-side (this chip isn't
      // emitted from tile_metrics.py — it's computed in JS from
      // tile.apps directly) and stash it in the same registry the
      // backend-produced chips use, so the popover code is shared.
      const why = used === 0
        ? `None of this bot's ${total} installed app${total === 1 ? '' : 's'} ${total === 1 ? 'has' : 'have'} been used in the last ${winLabel}.`
        : used === total
          ? `All ${total} apps installed on this bot have been used in the last ${winLabel}.`
          : `${used} of this bot's ${total} installed apps have been used in the last ${winLabel}; ${total - used} ${total - used === 1 ? 'has' : 'have'} been quiet.`;
      const impact = used === 0
        ? 'No app activity at all in this window. May mean the bot isn\'t being prompted to use its apps, the apps are scheduled and not yet due, or one or more apps are failing silently.'
        : used === total
          ? 'Every installed app is seeing use. No action needed — this is the healthy state.'
          : 'Quiet apps may be misconfigured, missing a schedule, or just out-of-season. They still count against the bot\'s complexity and audit surface, so it\'s worth a periodic look.';
      window._tileChipExplainers = window._tileChipExplainers || {};
      window._tileChipExplainers[id] = window._tileChipExplainers[id] || {};
      window._tileChipExplainers[id]['apps_usage'] = {
        id: 'apps_usage',
        severity: 'info',
        label: `${used}/${total} apps`,
        detail: `${used} of ${total} apps used in the last ${winLabel}`,
        why,
        impact,
        remediations: [
          // Primary action button — start a scan right from the popover.
          // For the "0/N" and "X<N" cases, refreshing the inventory is
          // often the right first step (a stale manifest can make a
          // running app look like it's idle). The N/N case still gets
          // the button — a fresh scan never hurts.
          { label: '↻ Run a scan now', kind: 'action', action: 'run_capability_scan' },
          { label: 'Open Apps page', kind: 'deep_link', nav: 'apps' },
        ],
        remediation_note: 'A scan refreshes the bot\'s app inventory. On the Apps page each app shows its last-use time, schedule (if any), and recent run status — the fastest way to see which app is the quiet one and why.',
      };
      return `<span class="pod-chip pod-chip-apps pod-chip-clickable" title="${used} of ${total} apps used in the last ${winLabel}" onclick="openChipExplainer('${escHtml(id)}','apps_usage')">${used}/${total} apps</span>`;
    })();
    // Chips inner HTML (no wrapper) — assembled here so the actions
    // block can either drop them into their own row (breaker states)
    // or fuse them with the ⋯ menu into a single inline row (normal
    // state) without rendering twice.
    const chipsInner = (() => {
      let healthHtml = '';
      // When L2 is tripped, the bot is intentionally offline. Suppress
      // the cached "✓ Healthy" default (and any tile_metrics chips
      // that don't yet know about breakers) and show a single
      // unambiguous "halted" chip instead. The breaker pill at the
      // top of the tile + the Reactivate button below carry the
      // actionable detail; this chip is just the at-a-glance read.
      if (_l2Tripped) {
        healthHtml = `<span class="pod-chip pod-chip-crit">⚡ Halted</span>`;
      } else if (tile) {
        if (!tile.health_chips || tile.health_chips.length === 0) {
          healthHtml = `<span class="pod-chip pod-chip-ok">✓ Healthy</span>`;
        } else {
          healthHtml = tile.health_chips.map(c => {
            // Stash chip data on a global registry keyed by bot_id +
            // chip_id so the explainer popover (openChipExplainer) can
            // look up `why` / `impact` / `remediations` / `detail` on
            // click without re-fetching. Per
            // docs/principle-alerts-explain-and-remediate.md, chips
            // that carry the `why` field open the explainer popover;
            // legacy chips with `nav` still navigate directly (the
            // principle is migrated incrementally).
            const hasExplainer = !!c.why;
            if (hasExplainer) {
              window._tileChipExplainers = window._tileChipExplainers || {};
              window._tileChipExplainers[id] = window._tileChipExplainers[id] || {};
              window._tileChipExplainers[id][c.id] = c;
            }
            // click_action: free-form dispatcher key for chips that need
            // a custom click target (a per-bot modal, a specific page +
            // pre-selection, etc.). Currently used by the
            // ``setup_progress`` chip — backend appends this field; the
            // dispatch below opens the setup-checklist modal for the
            // tile's bot. Add new dispatches here as needed.
            const hasClickAction = typeof c.click_action === 'string' && c.click_action.length > 0;
            const clickable = c.nav || hasExplainer || hasClickAction;
            const cls = `pod-chip pod-chip-${c.severity}` + (clickable ? ' pod-chip-clickable' : '');
            const title = escHtml(c.detail || '');
            const label = escHtml(c.label);
            if (hasExplainer) {
              return `<span class="${cls}" title="${title}" onclick="openChipExplainer('${escHtml(id)}','${escHtml(c.id)}')">${label}</span>`;
            }
            if (hasClickAction) {
              if (c.click_action === 'modal:setup_checklist') {
                return `<span class="${cls}" title="${title}" onclick="openSetupChecklistModal('${escHtml(id)}')">${label}</span>`;
              }
              // Unknown click_action — degrade to a static chip rather
              // than rendering nothing so the operator still sees state.
              return `<span class="${cls}" title="${title}">${label}</span>`;
            }
            if (c.nav) {
              const nav = escHtml(c.nav);
              return `<span class="${cls}" title="${title}" onclick="chipNav('${nav}')">${label}</span>`;
            }
            return `<span class="${cls}" title="${title}">${label}</span>`;
          }).join('');
        }
      }
      return healthHtml + appsChip;
    })();
    const sessionBit = b.oc_sessions_active != null ? `· ${b.oc_sessions_active} active ` : '';
    const timeBit = isPrimary ? '' : (b.last_metric_date ? ago(new Date(b.last_metric_date)) : '—');
    const uptimeBit = (() => {
      if (!isPrimary || b.admin_uptime_seconds == null) return '';
      const s = b.admin_uptime_seconds;
      if (s < 60) return `up ${s}s`;
      if (s < 3600) return `up ${Math.floor(s/60)}m`;
      if (s < 86400) return `up ${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m`;
      return `up ${Math.floor(s/86400)}d ${Math.floor((s%86400)/3600)}h`;
    })();
    // Evolve version sync badge — show the DATE segment (e.g. "Jun 14") to keep
    // the header compact AND legible. The old abbreviation showed the trailing
    // PR number ("v2884"), which reads as a sequence position even though it's
    // an identifier assigned at PR creation — v2026.0614.2884 is NEWER than
    // v2026.0613.2885 despite 2884 < 2885 (D-2). Full version + build id live
    // in the tooltip via verBuildId/verDateLabel (core/dom-utils.js).
    const currentVer = _statusData.evolve_current_version;
    const deployedVer = b.evolve_version;
    const synced = b.evolve_synced;
    const verTip = v => { const bid = verBuildId(v); return `Evolve ${verDateLabel(v)}${bid ? ' · build ' + bid : ''} (v${v})`; };
    let verBadge = '';
    if (isPrimary) {
      if (currentVer) {
        verBadge = `<span class="pod-ver-badge pod-ver-ok" title="Primary running ${escHtml(verTip(currentVer))}">${escHtml(verBadgeLabel(currentVer))}</span>`;
      }
    } else {
      if (deployedVer == null) {
        verBadge = `<span class="pod-ver-badge pod-ver-unknown" title="Never stamped — run upgrade to record version">v?</span>`;
      } else if (synced) {
        verBadge = `<span class="pod-ver-badge pod-ver-ok" title="Running current ${escHtml(verTip(deployedVer))}">${escHtml(verBadgeLabel(deployedVer))}</span>`;
      } else if (b.evolve_relation === 'ahead') {
        // Newer commit than the admin server (identity-based) — not outdated.
        verBadge = `<span class="pod-ver-badge pod-ver-unknown" title="Running ${escHtml(verTip(deployedVer))} — a newer build than the admin server itself; nothing to do.">${escHtml(verBadgeLabel(deployedVer))}</span>`;
      } else {
        // Behind / unknown. In direct mode the repo-puller auto-redeploys, so
        // this is a calm "updating" window — no ⚠, no "click to upgrade" (which
        // would race the auto-redeploy and, in the non-monotonic case, point at
        // a lower version number). Mirrors the Maintenance System badge.
        verBadge = `<span class="pod-ver-badge pod-ver-warn" title="Running ${escHtml(verTip(deployedVer))} — waiting to update to ${escHtml(verDateLabel(currentVer))} (v${escHtml(currentVer)}); bots update automatically, no manual upgrade needed.">${escHtml(verBadgeLabel(deployedVer))} · updating</span>`;
      }
    }
    // Per-tile action surface — single ⋯ menu collapses every action
    // (Redeploy/Restart/Verify/App Scan/Security Check/Backup/CE toggle/
    // Circuit Breakers/Remove) into one grouped popover so the tile face
    // stays a pure status scan. The breaker pill in the header row
    // continues to carry the "what is tripped" status; menu's "⚡
    // Circuit Breakers" item is the equivalent of the old inline ⚡
    // button.
    //
    // When L2 (full halt) is tripped the only meaningful action is
    // "turn it back on", so we keep a big Reactivate CTA inline above
    // the menu. Same shape (CTA + menu) when L1 is tripped — gateway
    // is up so the operator may still want to Redeploy/Restart from
    // the menu, but Reactivate is the headline action.
    const _bigReactivateBtn = `<button class="btn btn-primary" style="width:100%;padding:12px;font-size:0.95rem;font-weight:600;background:rgba(61,217,132,0.20);border:1px solid var(--green,#3dd984);color:var(--green,#3dd984)"
        onclick="reactivateBotFromTile('${id}')"
        title="Clear active breakers and return ${escHtml(botLabel(id))} to normal">🔄 Reactivate this bot</button>`;
    const _menuHtml = _podTileMenuHtml(id, b, isPrimary);
    // Breaker states keep chips on their own row above the Reactivate
    // CTA; normal state fuses chips and menu into a single inline row
    // (chips left-aligned, menu pushed right via margin-left:auto) so
    // the action surface stops eating a whole line of vertical space.
    const _chipsRow = chipsInner ? `<div class="pod-chips">${chipsInner}</div>` : '';
    const actions = _l2Tripped
      ? `${_chipsRow}<div class="pod-actions" style="display:block">${_bigReactivateBtn}</div>`
      : _anyBreaker
        ? `${_chipsRow}<div class="pod-actions" style="flex-direction:column;align-items:stretch">
             ${_bigReactivateBtn}
             <div style="display:flex;justify-content:flex-end;margin-top:6px">${_menuHtml}</div>
           </div>`
        : `<div class="pod-chips" style="align-items:center">${chipsInner}<span style="margin-left:auto;display:inline-flex">${_menuHtml}</span></div>`;
    const isMultiUser = b.multiUser === true;
    const accessTip = isMultiUser
      ? 'Shared: bot serves multiple users. Routes improvement proposals to pod_operator, scopes user profiles per user_key, and runs multi-user posture checks (admin claimed, primary recorded, exec scoped). Click to switch to 1:1.'
      : 'Personal: bot serves one user. Routes proposals to its primary, keeps a single user profile, and skips multi-user posture checks. Click to switch to shared.';
    const accessPill = isPrimary ? '' :
      `<span title="${accessTip}"
        onclick="toggleBotMultiUser('${id}', ${isMultiUser})" style="cursor:pointer;margin-left:6px;font-size:0.62rem;padding:1px 6px;border-radius:10px;border:1px solid ${isMultiUser ? 'var(--yellow)' : 'var(--text3)'};color:${isMultiUser ? 'var(--yellow)' : 'var(--text3)'};white-space:nowrap;user-select:none">${isMultiUser ? 'shared' : '1:1'}</span>`;
    // Pending proposals badge — click navigates to Self-Improvement
    const proposalCount = b.proposal_count || 0;
    const proposalBadge = (!isPrimary && proposalCount > 0)
      ? `<span title="${proposalCount} pending improvement proposal${proposalCount !== 1 ? 's' : ''} for ${id} — click to review"
           onclick="nav(document.querySelector('[data-page=self-improvement]'))"
           style="cursor:pointer;margin-left:6px;font-size:0.62rem;padding:1px 6px;border-radius:10px;background:rgba(251,191,36,0.15);border:1px solid var(--yellow);color:var(--yellow);white-space:nowrap;user-select:none">⟳ ${proposalCount}</span>`
      : '';
    // API key status pill
    const keyStatus = b.key_status;
    const keyProviders = b.key_providers || {};
    const keyTip = keyStatus === 'ok'
      ? 'API key configured: ' + Object.entries(keyProviders).filter(([,v]) => v.api_key || v.token).map(([p]) => p).join(', ')
      : keyStatus === 'missing' ? 'No API key found — bot cannot make LLM calls'
      : 'Key status unknown';
    const keyPill = (!isPrimary && keyStatus)
      ? `<span title="${keyTip}" style="margin-left:6px;font-size:0.62rem;padding:1px 6px;border-radius:10px;border:1px solid ${keyStatus === 'ok' ? 'var(--text3)' : 'var(--red)'};color:${keyStatus === 'ok' ? 'var(--text3)' : 'var(--red)'};white-space:nowrap">${keyStatus === 'ok' ? '🔑 ok' : keyStatus === 'missing' ? '🔑 missing' : '🔑 ?'}</span>`
      : '';
    // Circuit-breaker pill (Phase 4b). Only renders when at least one
    // active breaker exists on this bot or pod-wide. Red for L2 ("full"
    // halt — bot is offline); orange for L1 ("cost" — background activity
    // blocked). Click opens the breaker modal scoped to this bot. The
    // type-set declarations were hoisted above so the action button can
    // share them; here we just consume them.
    let breakerPill = '';
    if (_anyBreaker) {
      const hasFull = _breakerTypes.has('full') || _podBreakerTypes.has('full');
      const hasCost = _breakerTypes.has('cost') || _podBreakerTypes.has('cost');
      const isPod = _podBreakerTypes.size > 0 && _breakerTypes.size === 0;
      const color = hasFull ? 'var(--red,#f87171)' : 'var(--yellow,#fbbf24)';
      const bg = hasFull ? 'rgba(248,113,113,0.18)' : 'rgba(251,191,36,0.18)';
      const label = hasFull && hasCost
        ? '⚡ breakers tripped'
        : hasFull ? '⚡ halted' : '⚡ cost breaker';
      const scopeNote = isPod ? ' (pod-wide)' : '';
      const tip = `Circuit breaker tripped${scopeNote}. Click to view / reset.`;
      // When the pill is showing solely because of a pod-wide trip,
      // route the click to the pod-scoped modal so it doesn't open a
      // bot-scoped modal that reads "No active breakers". Per-bot
      // trips (with or without a concurrent pod-wide trip) keep the
      // bot scope so the modal lists this bot's records first.
      breakerPill = `<span title="${tip}"
           onclick="openBreakerModal('${isPod ? 'pod' : id}', ${isPod})"
           style="cursor:pointer;margin-left:6px;font-size:0.62rem;padding:1px 6px;border-radius:10px;background:${bg};border:1px solid ${color};color:${color};white-space:nowrap;user-select:none">${label}</span>`;
    }
    // Tile-wide visual when any breaker is tripped on this bot or pod-wide.
    // Red border + faint background tint makes "this bot is impaired" the
    // first thing the operator sees on the dashboard. Pod-wide trips
    // mark every tile because pod-wide IS the per-bot state too.
    const _tileBreakerStyle = _anyBreaker
      ? 'border-color:var(--red,#f87171);background:linear-gradient(180deg,rgba(248,113,113,0.06),rgba(248,113,113,0.02))'
      : '';

    // Countdown to auto-recovery — derived from the earliest expires_at
    // across the bot's active breakers (plus any pod-wide ones, since
    // those also halt this bot). Indefinite trips show "manual reset
    // required". A short JS chunk re-computes the relative time on each
    // render; the dashboard polls /api/status every 5s so the value
    // refreshes naturally without a separate timer.
    let breakerCountdownHtml = '';
    if (_anyBreaker) {
      const _allTrips = [
        ...(b.active_breakers || []),
        ...((_statusData?.pod_breakers) || []),
      ];
      const _withExpiry = _allTrips
        .map(t => t.expires_at ? new Date(t.expires_at).getTime() : null)
        .filter(t => t !== null && !isNaN(t));
      const _hasIndefinite = _allTrips.some(t => !t.expires_at);
      let _label;
      if (_withExpiry.length === 0) {
        _label = 'Indefinite — manual reset required';
      } else {
        const _earliest = Math.min(..._withExpiry);
        const _mins = Math.max(0, Math.round((_earliest - Date.now()) / 60000));
        const _txt = _mins <= 0 ? 'any moment now'
          : _mins < 60 ? `${_mins}m`
          : _mins < 1440 ? `${Math.floor(_mins / 60)}h ${_mins % 60}m`
          : `${Math.floor(_mins / 1440)}d ${Math.floor((_mins % 1440) / 60)}h`;
        _label = `Reactivates in ${_txt}`
          + (_hasIndefinite ? ' (+ indefinite trip present)' : '');
      }
      breakerCountdownHtml = `<div style="margin-top:6px;font-size:0.74rem;color:var(--red,#f87171);font-weight:500">⏱ ${escHtml(_label)}</div>`;
    }

    // Diagnosis-available indicator — populated by the Phase 5c async
    // audit-of-cause generator. Until that ships, audit_summary stays
    // null and this chip stays hidden. When present, clicking it opens
    // the breaker modal, which now renders the diagnosis section.
    const _hasDiagnosis = (b.active_breakers || []).some(t => t.audit_summary)
      || ((_statusData?.pod_breakers) || []).some(t => t.audit_summary);
    const breakerDiagnosisHtml = _hasDiagnosis
      ? `<button class="btn btn-ghost btn-sm" onclick="openBreakerModal('${id}', false)" style="margin-top:4px;font-size:0.72rem;padding:2px 8px;color:var(--text,#e6e7eb);border-color:rgba(255,255,255,0.2)" title="A diagnosis of why this breaker tripped is available — click to view">📋 View diagnosis</button>`
      : '';

    return `<div class="pod-node" style="${_tileBreakerStyle}">
      <div class="pod-status-col"><div class="pod-ring ${ringClass}"></div></div>
      <div class="pod-body">
        <div class="pod-header"><span class="pod-name">${escHtml(botLabel(id))}</span>${modelTag}${verBadge}${accessPill}${proposalBadge}${keyPill}${breakerPill}</div>
        <div class="pod-meta">${escHtml(b.role)} · port ${b.port || '?'} ${sessionBit}${timeBit ? `· ${timeBit}` : ''}${uptimeBit ? `· ${uptimeBit}` : ''}</div>
        ${breakerCountdownHtml}${breakerDiagnosisHtml}
        ${activityHtml}${costHtml}${actions}
      </div>
    </div>`;
  }
}

// ── Shared lag-state selector ───────────────────────────────────────────────
// Single source of truth for "how far is the fleet behind the promoted release
// pointer". Three consumers used to recompute this independently: the direct-
// mode drawer section, the canary drawer section, and the _classifyUpdates
// updates-cell derivation. They agreed, but the duplicated logic was a drift
// risk — so the shared core lives here once.
//
// Pure and deterministic in (bots, rel): no module globals, no wall-clock.
// "Members" are the bots that should be on the pointer — non-primary, and
// excluding the canary/soak bot, which runs ahead by design. Under canary,
// evolve_synced is recomputed server-side against the stable pointer, so a
// `false` here means "lagging the gated pointer" (a genuine deploy failure),
// not normal soak skew. In direct mode release_ui_view() returns None, so rel
// is null/absent → canaryBot resolves to null → no exclusion, matching the
// legacy per-bot version-equality member set exactly.
//
// stableV is the raw promoted-pointer version (rel.stable_version) or null.
// _classifyUpdates layers its own direct-mode `evolve_current_version` fallback
// on top of this (a label-only fallback its banner siblings never applied), so
// this selector intentionally returns the un-fallen-back value.
//
// NOT included: the transient-vs-stuck grace classification (the D-3 fold). It
// needs the git-backed _releaseStatus snapshot and Date.now(), is consumed only
// by _classifyUpdates, and would break this function's purity — so it stays
// local there, derived from this selector's nBehind plus the promoted_at stamp.
function lagState(bots, rel) {
  const canaryBot = (rel && rel.canary_bot) || null;
  const members = Object.entries(bots || {}).filter(
    ([id, b]) => b.role !== 'primary' && id !== canaryBot);
  // A bot that runs a NEWER commit than the admin server (evolve_relation
  // 'ahead', set by apply_release_status from the identity-based compare) is
  // not "behind" — exclude it so it never inflates the lag count or trips the
  // "updating" banner. Only genuinely-behind / unknown stamps count.
  const behind = members.filter(
    ([, b]) => b.evolve_synced === false && b.evolve_relation !== 'ahead');
  // Return only the fields the three consumers read: `behind` (the [id,bot]
  // pairs, used for both .length and name lists), `nBehind`, `total`, and the
  // raw promoted-pointer `stableV`. `members` stays internal (it's just the
  // denominator behind/total are derived from).
  return {
    behind,
    nBehind: behind.length,
    total: members.length,
    stableV: (rel && rel.stable_version) || null,
  };
}

// ── Consolidated "Release & update" drawer ─────────────────────────────────
// One card in the #ov-updates-detail drawer body that merges what used to be
// three stacked, partly-redundant boxes: the canary release-pipeline panel, the
// Evolve update/soak banner, and the OpenClaw upgrade banner. They describe TWO
// update tracks — the Evolve fleet release pointer and the OpenClaw runtime — so
// the card renders them as stacked rows with a SINGLE source of truth per fact
// (e.g. one soak ETA, from the git-backed snapshot; previously the panel and the
// banner each computed it and could disagree). #ov-sync-banner is no longer an
// update-state surface: it is reserved for live promote/rollback job progress
// (rendered below the card, still inside the drawer). The contract-bound summary
// cell + slim loud banner (_classifyUpdates / _refreshUpdatesCell, D-8) are
// untouched — this only consolidates the expanded-drawer presentation.
function renderUpdatesDrawer() {
  const panel = document.getElementById('ov-release-panel');
  if (!panel) return;
  const sd = _statusData || {};
  const rel = sd.release || {};
  const canary = rel.mode === 'canary';

  // Canary detail needs the git-backed /api/release/status snapshot; refresh it
  // when missing or stale (≥15s). The render below is synchronous; the async
  // fetch re-renders via renderUpdatesDrawer() on completion (refreshReleaseStatus).
  if (canary && (!_releaseStatus || (Date.now() - _releaseStatusAt) > 15000)) {
    refreshReleaseStatus();
  }

  // Evolve track (release pointer): canary pipeline detail OR the direct-mode
  // tiered update banner. OpenClaw track: the runtime upgrade row, mode-
  // independent (shows in direct mode too, where the Evolve row is often empty).
  const evolveRow = canary ? _drawerCanarySection() : _drawerDirectSection(sd);
  const oc = _ovOcData;
  const ocRow = (oc && oc.update_available && typeof renderOverviewOcBanner === 'function')
    ? renderOverviewOcBanner(oc)
    : '';

  const rows = [evolveRow, ocRow].filter(Boolean);
  if (!rows.length) { panel.style.display = 'none'; panel.innerHTML = ''; return; }
  panel.style.display = 'block';
  // Each track is its own section; a hairline separator divides the two when
  // both are present. The drawer's own <summary> already titles the card
  // ("Release & update detail"), so no inner card-title is repeated here.
  panel.innerHTML = rows
    .map((r, i) => `<div${i ? ' style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border)"' : ''}>${r}</div>`)
    .join('');
}

// Direct-mode (non-canary) Evolve update row. Tier-aware (security / feature /
// maintenance) per docs/spec-release-tiers-2026-05-16.md. Returns an HTML string
// for the drawer card (was renderEvolveSyncBanner's #ov-sync-banner write), or
// '' when the fleet is current or the notice is snoozed.
function _drawerDirectSection(sd) {
  // Direct mode: rel is null (release_ui_view returns None unless canary), so
  // lagState's canary-bot exclusion is a no-op — the member set is exactly the
  // "all non-primary bots" computation.
  const lag = lagState(sd.bots || {}, null);
  const release = sd.latest_release;
  if (!lag.behind.length || !sd.evolve_current_version) return '';

  // Default to "maintenance" when the server didn't resolve a release entry.
  const tier = (release && release.tier) || 'maintenance';
  const version = (release && release.version) || sd.evolve_current_version;
  const headline = release && release.headline;
  const details = release && release.details;
  const link = release && release.link;

  // Snooze: localStorage, keyed by tier. security never snoozes.
  if (tier !== 'security') {
    const until = parseInt(localStorage.getItem(`evolve.releaseSnooze.${tier}`) || '0', 10);
    if (until && Date.now() < until) return '';
  }

  const botCountLabel = `${lag.behind.length} of ${lag.total} bot${lag.total === 1 ? '' : 's'} waiting to update`;
  const expandedNames = lag.behind.map(([id]) => escHtml(id)).join(', ');
  const linkHtml = link ? ` <a href="${escHtml(link)}" target="_blank" rel="noopener" style="color:inherit;text-decoration:underline">Details</a>` : '';

  // Tier-specific style tokens (severity color carried on a contained sub-box).
  const styles = {
    security:    { bg: 'rgba(248,113,113,0.12)', border: 'rgba(248,113,113,0.5)', color: 'var(--red)',    icon: '🛡', verb: 'Apply now', btnBg: 'var(--red)',    btnColor: '#fff', snoozable: false, padding: '12px 16px', fontSize: '0.85rem' },
    feature:     { bg: 'rgba(251,191,36,0.10)',  border: 'rgba(251,191,36,0.30)', color: 'var(--yellow)', icon: '↑',  verb: 'Update',    btnBg: 'var(--yellow)', btnColor: '#000', snoozable: true,  padding: '10px 14px', fontSize: '0.8rem'  },
    maintenance: { bg: 'rgba(124,92,255,0.07)', border: 'rgba(124,92,255,0.28)', color: 'var(--purple)', icon: '↑',  verb: 'Update',    btnBg: 'rgba(124,92,255,0.15)', btnColor: 'var(--purple)', snoozable: true, padding: '6px 12px', fontSize: '0.72rem' },
  };
  const s = styles[tier] || styles.maintenance;

  let body;
  if (tier === 'security') {
    body = `<div style="display:flex;flex-direction:column;gap:2px">
        <span><strong>${s.icon} Security update — apply now</strong>${headline ? ' — ' + escHtml(headline) : ''}</span>
        ${details ? `<span style="opacity:0.85;font-size:0.78rem">${escHtml(details)}</span>` : ''}
        <span style="opacity:0.7;font-size:0.72rem" title="${escHtml(expandedNames)}">${botCountLabel}${linkHtml}</span>
      </div>`;
  } else if (tier === 'feature') {
    body = `<div style="display:flex;flex-direction:column;gap:2px">
        <span><strong>${s.icon} New in v${escHtml(version)}</strong>${headline ? ' — ' + escHtml(headline) : ''}</span>
        ${details ? `<span style="opacity:0.85;font-size:0.74rem">${escHtml(details)}</span>` : ''}
        <span style="opacity:0.7;font-size:0.7rem" title="${escHtml(expandedNames)}">${botCountLabel}${linkHtml}</span>
      </div>`;
  } else {
    body = `<span title="${escHtml(expandedNames)}">${s.icon} Evolve v${escHtml(version)} available · ${botCountLabel}${linkHtml}</span>`;
  }

  const snoozeHtml = s.snoozable
    ? `<button class="btn btn-ghost btn-sm" onclick="snoozeUpgrade('${tier}')" style="margin-left:6px;font-size:0.7rem;opacity:0.7" title="Hide this notice for a while">Snooze</button>`
    : '';
  const upgradeBtn = `<button class="btn btn-sm" style="margin-left:auto;background:${s.btnBg};color:${s.btnColor};border:1px solid ${s.border};white-space:nowrap" onclick="sysmUpgrade(null)">${s.icon} ${s.verb}</button>`;

  return `<div style="display:flex;align-items:center;gap:10px;background:${s.bg};border:1px solid ${s.border};color:${s.color};padding:${s.padding};border-radius:8px;font-size:${s.fontSize}">${body}${upgradeBtn}${snoozeHtml}</div>`;
}

function snoozeUpgrade(tier) {
  // security never snoozes — server-side rendering would have excluded it,
  // but guard here anyway in case a stale render fires this.
  if (tier === 'security') return;
  const days = tier === 'feature' ? 7 : 30;
  const until = Date.now() + days * 24 * 60 * 60 * 1000;
  localStorage.setItem(`evolve.releaseSnooze.${tier}`, String(until));
  renderUpdatesDrawer();   // drop the now-snoozed row from the card
}

// ── Canary release-pipeline section (drawer card) ──────────────────────────
// Under pod.release.mode == "canary" the fleet is held on the gated stable
// pointer while a newer candidate soaks. Returns the HTML for the Evolve track
// row of the consolidated drawer card: promoted-pointer status, distance from
// origin/main tip (NEUTRAL — being behind tip is the by-design canary state),
// the in-flight candidate with a SINGLE soak ETA (git-backed
// soak_minutes_remaining), pin / degraded notes, a genuine-lag warning (the
// former "Path B", a real deploy failure — version-keyed Acknowledge; the loud
// banner mirrors it above the grid), and ALL release controls co-located:
// "Make live now" (early promote, soaking only) + Undo last update + Pause/Resume auto-updates.
// Every action POSTs to /api/release/* which the admin server runs in-process as
// the evolve user (it owns release.json + the fleet checkout) — ZERO sudo, no
// password. NEVER surface a `sudo evolve-admin …` hint here.
function _drawerCanarySection() {
  const sd = _statusData || {};
  const rel = sd.release || {};
  const st = _releaseStatus;
  if (!st) return `<div style="color:var(--text2);font-size:0.8rem">Loading update status…</div>`;
  if (st.state === 'CORRUPT') {
    return `<div><span class="badge badge-crit">Update config unreadable — updates paused</span></div>`
      + `<div style="color:var(--text2);font-size:0.72rem;margin-top:6px">${escHtml(st.error || '')}</div>`;
  }

  const stable = st.stable || {};
  const stableV = stable.version || null;
  const stableSha = (stable.sha || '').slice(0, 12);
  const stableDate = (st.recency && st.recency.stable_commit_date) || stable.commit_date || null;

  // Current-version (fleet) line — what the gated checkout actually runs.
  const promotedLine = `<div style="font-size:0.8rem">`
    + `<span style="color:var(--text2)">Current version</span> `
    + `<strong>${stableV ? escHtml(verDateLabel(stableV)) : '—'}</strong>`
    + (stableSha ? ` <span class="badge badge-neutral" style="font-family:monospace">${escHtml(stableSha)}</span>` : '')
    + (stableDate ? ` <span style="color:var(--text2)">· committed ${escHtml(stableDate)}</span>` : '')
    + (stableV ? ` <span style="color:var(--text3)">· v${escHtml(stableV)}</span>` : '')
    + `</div>`;

  // Newer-changes badge — NEUTRAL (by-design tested-update state); up-to-date earns green.
  const behindN = st.behind_origin;
  let behindBadge, behindNote = '';
  if (behindN == null) {
    behindBadge = `<span class="badge badge-neutral">Newest available version unknown</span>`;
  } else if (behindN <= 0) {
    behindBadge = `<span class="badge badge-ok">Up to date with the newest version</span>`;
  } else {
    behindBadge = `<span class="badge badge-neutral">${behindN} newer change${behindN === 1 ? '' : 's'} not yet live</span>`;
    behindNote = `<div style="color:var(--text2);font-size:0.72rem;margin-top:3px">`
      + `The fleet runs the current tested version, not the newest code — `
      + `new changes are tested before they go live.</div>`;
  }
  const behindLine = `<div style="margin-top:8px">${behindBadge}${behindNote}</div>`;

  // Candidate in flight (gating / soaking / failed) — ONE soak ETA, server-
  // computed (st.soak_minutes_remaining); the old separate banner ETA is gone.
  const cand = st.candidate || null;
  const soaking = !!(cand && cand.state === 'soaking');
  let candLine = '';
  if (cand && cand.sha) {
    const cSha = (cand.sha || '').slice(0, 12);
    let label, cls;
    if (cand.state === 'soaking') {
      const left = (typeof st.soak_minutes_remaining === 'number')
        ? (st.soak_minutes_remaining <= 0 ? 'finishing tests' : `~${st.soak_minutes_remaining}m left`)
        : 'testing';
      label = `Update testing — ${left}`; cls = 'badge-primary';
    } else if (cand.state === 'checking') {
      label = 'Update — running checks'; cls = 'badge-neutral';
    } else if (cand.state === 'failed') {
      label = 'Update blocked — failed checks'; cls = 'badge-crit';
    } else {
      label = `Update ${cand.state || ''}`; cls = 'badge-neutral';
    }
    const aheadPhrase = commitsAheadPhrase(cand.commits_ahead);
    candLine = `<div style="margin-top:8px;font-size:0.8rem">`
      + `<span class="badge ${cls}">${escHtml(label)}</span> `
      + `<span class="badge badge-neutral" style="font-family:monospace">${escHtml(cSha)}</span>`
      + (cand.commit_date ? ` <span style="color:var(--text2)">· committed ${escHtml(cand.commit_date)}</span>` : '')
      + (aheadPhrase ? ` <span style="color:var(--text2)">· ${escHtml(aheadPhrase)} of the fleet</span>` : '')
      + `</div>`
      + (soaking ? `<div style="color:var(--text2);font-size:0.72rem;margin-top:3px">Goes live to the fleet automatically when testing passes — no action needed, or “Make live now” to skip the wait.</div>` : '')
      + (cand.state === 'checking' ? `<div style="color:var(--text2);font-size:0.72rem;margin-top:3px">Checking the update before testing starts. No action needed.</div>` : '');
  }

  // Pinned-state badge.
  const pin = st.pin || null;
  const pinLine = pin
    ? `<div style="margin-top:8px"><span class="badge badge-warn">Auto-updates paused</span>`
      + (pin.reason ? ` <span style="color:var(--text2);font-size:0.72rem">${escHtml(pin.reason)}</span>` : '')
      + `</div>`
    : '';

  const degradedLine = st.degraded_no_canary
    ? `<div style="margin-top:6px;color:var(--text3);font-size:0.72rem">No test bot configured — timer-only testing.</div>`
    : '';

  // Genuine lag behind the stable pointer (a real deploy failure). Version-keyed
  // Acknowledge; the loud banner already surfaces it above the grid.
  const { behind: lagBehind, total } = lagState(sd.bots || {}, rel);
  let lagLine = '';
  if (lagBehind.length && stableV && !_isCanaryLagAcked(stableV)) {
    const names = lagBehind.map(([id]) => escHtml(id)).join(', ');
    const stableLabel = verDateLabel(stableV);
    const aheadOfPrev = commitsAheadPhrase(rel.stable_commits_ahead);
    const stableDetail = `v${escHtml(stableV)}`
      + (rel.stable_commit_date ? `, committed ${escHtml(rel.stable_commit_date)}` : '')
      + (aheadOfPrev ? `, ${escHtml(aheadOfPrev)} of the prior release` : '');
    lagLine = `<div style="display:flex;align-items:center;gap:10px;margin-top:10px;background:rgba(251,191,36,0.10);border:1px solid rgba(251,191,36,0.30);color:var(--yellow);padding:10px 14px;border-radius:8px;font-size:0.8rem">`
      + `<div style="display:flex;flex-direction:column;gap:2px">`
      + `<span><strong>⚠ ${lagBehind.length} of ${total} bot${total === 1 ? '' : 's'} still updating (${escHtml(stableLabel)})</strong></span>`
      + `<span style="opacity:0.85;font-size:0.72rem" title="${escHtml(names)}">Some bots haven't updated to the current version (${stableDetail}) yet — they update automatically; check the update status if this persists.</span>`
      + `</div>`
      + `<button class="btn btn-ghost btn-sm" onclick="ackCanaryLag('${escHtml(stableV)}')" style="margin-left:auto;font-size:0.7rem;opacity:0.75" title="Hide this notice until the next update">Acknowledge</button>`
      + `</div>`;
  }

  // Controls — every release action in one row. "Make live now" (early
  // promote, soaking only) sits with "Undo last update" (reversible-but-noisy)
  // and the Pause/Resume auto-updates toggle.
  const completeBtn = soaking
    ? `<button class="btn btn-sm" style="white-space:nowrap;background:rgba(124,92,255,0.15);color:var(--purple);border:1px solid rgba(124,92,255,0.4)" onclick="promoteRelease('${escHtml(cand.sha || '')}')" title="Skip the rest of testing and make this update live for the whole fleet now">Make live now</button>`
    : '';
  const hasPrev = !!(st.previous && st.previous.sha);
  const prevVer = (st.previous && st.previous.version) || null;
  const undoLabel = (hasPrev && prevVer) ? `Undo last update → v${escHtml(prevVer)}` : 'Undo last update';
  const rollbackBtn = `<button class="btn btn-warning btn-sm"${hasPrev ? '' : ' disabled'}`
    + ` onclick="rollbackRelease()" title="${hasPrev
        ? 'Roll the fleet back to the previous version (reversible; pauses auto-updates after)'
        : 'No previous version recorded yet — nothing to undo'}">${undoLabel}</button>`;
  const pinBtn = pin
    ? `<button class="btn btn-ghost btn-sm" onclick="togglePinRelease(false)" title="Resume automatic updates">Resume auto-updates</button>`
    : `<button class="btn btn-ghost btn-sm" onclick="togglePinRelease(true)" title="Pause automatic updates">Pause auto-updates</button>`;
  const controls = `<div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">${completeBtn}${rollbackBtn}${pinBtn}</div>`;

  return promotedLine + behindLine + candLine + pinLine + degradedLine + lagLine + controls;
}

// Acknowledge a behind-stable lag notice, keyed by stable version so the
// notice re-surfaces when the pointer next advances (a new version's lag is
// a new event worth re-flagging).
function ackCanaryLag(stableVersion) {
  if (!stableVersion) return;
  localStorage.setItem(`evolve.releaseAck.canary.${stableVersion}`, stableVersion);
  renderUpdatesDrawer();   // drop the now-acked lag warning from the card
  _refreshUpdatesCell();   // and clear the loud banner mirror
}

// Promote/rollback progress + terminal outcomes (incl. a failure step-log) are
// written to #ov-sync-banner, which now lives inside the collapsed-by-default
// updates-detail region. Force the region open so the operator always sees them
// — including the reload-fallback path where the persisted state would
// otherwise leave the region (and the message) collapsed and invisible.
function _openUpdatesRegion() {
  const r = document.getElementById('ov-updates-detail');
  if (r && !r.open) { r.open = true; _syncUpdatesCaret(); }
}

// "Make live now" — operator override that skips the remaining soak and
// promotes the soaking candidate to the fleet immediately. POSTs to
// /api/release/promote (the admin server runs it as evolve — no sudo). The
// promote redeploys the fleet and restarts THIS admin server, so we track it
// through the bounce rather than waiting on a single response.
async function promoteRelease(sha) {
  const short = (sha || '').slice(0, 12);
  if (!await confirmModal({ body: (`Make update ${short} live for the whole fleet now, skipping the `
    + `rest of testing?\n\nThe fleet updates and this admin server restarts — `
    + `the page reconnects on its own.`), danger: true })) return;
  const setBanner = (html, color) => {
    const b = document.getElementById('ov-sync-banner');
    if (!b) return;
    _openUpdatesRegion();
    b.style.display = 'flex';
    b.innerHTML = `<span${color ? ` style="color:${color}"` : ''}>${html}</span>`;
  };
  setBanner(`<strong>↑ Making ${escHtml(short)} live…</strong> updating the fleet; `
    + `the admin server will restart and this page will reconnect.`);
  api('POST', '/api/release/promote', {})
    .then(resp => {
      if (resp && resp.error) { setBanner(escHtml(resp.error), 'var(--red)'); return; }
      _watchPromoteJob(resp.jobId);
    })
    .catch(err => setBanner('Could not make live: ' + escHtml(String(err)), 'var(--red)'));
}

// Poll the promote job. Its hook suite SIGTERMs this admin server mid-job, so
// a 404 (job forgotten by the fresh process) or a network error is the EXPECTED
// success-in-progress signal — the release pointer is persisted before the
// hooks run, so the move is durable. An explicit "failed" status is a real
// failure; a "complete" status carrying result.info is a benign no-promote
// outcome (e.g. a newer candidate superseded the one we tried to promote).
function _watchPromoteJob(jobId) {
  if (!jobId) { _confirmPromoted(); return; }
  let tries = 0;
  const tick = setInterval(async () => {
    tries++;
    let j = null, restarting = false;
    try { j = await api('GET', '/api/jobs/' + encodeURIComponent(jobId)); }
    catch (_e) { restarting = true; }           // server bouncing between accept + response
    if (j && j.error && !j.status) restarting = true;  // job forgotten → already restarted
    if (j && j.status === 'failed') {
      clearInterval(tick);
      _renderPromoteOutcome(j, `<strong>Update didn't go live:</strong> `
        + `${escHtml(j.error || 'see the step log below')}`, 'var(--red)');
      return;
    }
    if (j && j.status === 'complete' && j.result && j.result.info) {
      // Benign candidate-replacement race (or other no-promote tick result):
      // nothing was promoted, but nothing failed either. Show it neutrally
      // with the step log — never the green "complete" or the red "failed".
      clearInterval(tick);
      _renderPromoteOutcome(j, `<strong>Not live yet —</strong> `
        + `${escHtml(j.result.info)}`, 'var(--blue)');
      return;
    }
    if ((j && j.status === 'complete') || restarting) { clearInterval(tick); _confirmPromoted(); return; }
    if (tries > 120) { clearInterval(tick); _confirmPromoted(); }  // ~4min safety stop
  }, 2000);
}

// Render a terminal promote outcome (real failure or benign no-promote) into
// the sync banner: a self-contained headline PLUS the job's step log. The
// promote flow has no separate drawer, so without this the operator would see
// a bare headline that references "steps" they can't see. `headlineHtml` is
// pre-escaped/marked-up by the caller; step lines come from the job log.
function _renderPromoteOutcome(job, headlineHtml, color) {
  const b = document.getElementById('ov-sync-banner');
  if (!b) return;
  _openUpdatesRegion();   // a failure step-log must not render into a collapsed region
  b.style.display = 'flex';
  const lines = (job && Array.isArray(job.log)) ? job.log : [];
  const steps = lines.length
    ? `<div style="display:flex;flex-direction:column;gap:2px;margin-top:4px;`
      + `font-size:0.72rem;color:var(--text2);max-height:160px;overflow:auto">`
      + lines.map(l => `<span>${escHtml((l && l.msg) || '')}</span>`).join('')
      + `</div>`
    : '';
  b.innerHTML = `<div style="display:flex;flex-direction:column;gap:4px;width:100%">`
    + `<span${color ? ` style="color:${color}"` : ''}>${headlineHtml}</span>`
    + `${steps}</div>`;
}

// Wait for the admin server to come back, then re-render from fresh state —
// the banner clears itself once the candidate is promoted (no full reload).
// `headlineHtml` overrides the default promote wording (rollback reuses this
// same restart-then-reconnect flow); pre-escaped/marked-up by the caller.
function _confirmPromoted(headlineHtml) {
  const b = document.getElementById('ov-sync-banner');
  const head = headlineHtml || `<strong>✓ Update is live</strong> — admin server `
    + `restarting; reconnecting…`;
  if (b) { _openUpdatesRegion(); b.innerHTML = `<span>${head}</span>`; }
  let tries = 0;
  const tick = setInterval(async () => {
    tries++;
    try {
      const st = await api('GET', '/api/status');
      if (st && !st.error) {
        clearInterval(tick);
        if (typeof loadStatus === 'function') loadStatus(); else location.reload();
        return;
      }
    } catch (_e) { /* still restarting — keep waiting */ }
    if (tries > 60) { clearInterval(tick); location.reload(); }  // ~2min fallback
  }, 2000);
}

// ── Release control panel (D-5) ────────────────────────────────────────────
// Read-only pointer / candidate / behind-tip status + operator Rollback and
// Pin controls for the gated release pipeline, on the Overview. Server-side
// only: the admin server runs as the evolve user and owns release.json + the
// fleet checkout, so these run with ZERO sudo (the same reason "Make live
// now" needs none). NEVER surface a `sudo evolve-admin …` hint here — the
// in-app Terminal runs as the passwordless evolve user. Promote-early stays the
// soak-banner's "Make live now"; this panel sits beside it and adds the
// status read + rollback + pin/unpin.
//
// Direct-mode pods have no release pointer: the panel hides and the git-backed
// status fetch is skipped entirely (gated on the already-polled
// _statusData.release.mode). Under canary the panel self-refreshes its
// git-backed snapshot on a slow staleness window, so renderOverview() (and tile
// toggles) stay synchronous and never shell out to git per render.
let _releaseStatus = null;        // last GET /api/release/status snapshot
let _releaseStatusAt = 0;         // Date.now() of that snapshot
let _releaseActionBusy = false;   // guards overlapping pin / rollback clicks

async function refreshReleaseStatus() {
  if (refreshReleaseStatus._inflight) return;
  refreshReleaseStatus._inflight = true;
  // Stamp the attempt up-front: a failing fetch leaves _releaseStatus null, and
  // renderUpdatesDrawer() (called below) would otherwise see "no snapshot" and
  // re-trigger immediately — a tight retry loop. The staleness gate keys off
  // this timestamp, so a failure now throttles the next attempt by ~15s too.
  _releaseStatusAt = Date.now();
  try {
    const st = await api('GET', '/api/release/status');
    if (st && !st.error) _releaseStatus = st;
  } catch (_e) { /* keep the last good snapshot */ }
  finally {
    refreshReleaseStatus._inflight = false;
    renderUpdatesDrawer();
    // The git-backed snapshot carries pin / candidate detail the cheap
    // _statusData.release lacks, so refresh the cell once it lands.
    _refreshUpdatesCell();
  }
}

// Roll the fleet back to the previous promoted release. POSTs to
// /api/release/rollback; like promote, the hook suite redeploys the fleet and
// restarts THIS admin server, so we track the job through the bounce.
async function rollbackRelease() {
  if (_releaseActionBusy) return;
  const st = _releaseStatus || {};
  const prev = ((st.previous && st.previous.sha) || '').slice(0, 12);
  if (!await confirmModal({ body: (`Roll the fleet back to the previous version${prev ? ' (' + prev + ')' : ''}?\n\n`
    + `The fleet updates and this admin server restarts — the page reconnects `
    + `on its own. Auto-updates are paused after a rollback; resume them `
    + `from this panel when you're ready.`), danger: true })) return;
  _releaseActionBusy = true;
  const setBanner = (html, color) => {
    const b = document.getElementById('ov-sync-banner');
    if (!b) return;
    _openUpdatesRegion();
    b.style.display = 'flex';
    b.innerHTML = `<span${color ? ` style="color:${color}"` : ''}>${html}</span>`;
  };
  setBanner(`<strong>↩ Rolling back…</strong> updating the fleet; the admin `
    + `server will restart and this page will reconnect.`);
  api('POST', '/api/release/rollback', {})
    .then(resp => {
      if (resp && resp.error) { setBanner(escHtml(resp.error), 'var(--red)'); _releaseActionBusy = false; return; }
      _watchRollbackJob(resp.jobId);
    })
    .catch(err => { setBanner('Rollback failed: ' + escHtml(String(err)), 'var(--red)'); _releaseActionBusy = false; });
}

// Poll the rollback job. Its hook suite SIGTERMs this admin server mid-job, so
// a 404 / network error is the EXPECTED success-in-progress signal (the pointer
// is persisted before the hooks run, so the move is durable). Mirrors
// _watchPromoteJob; rollback has no benign-race "info" outcome.
function _watchRollbackJob(jobId) {
  const done = `<strong>✓ Rollback complete</strong> — admin server restarting; reconnecting…`;
  if (!jobId) { _releaseActionBusy = false; _confirmPromoted(done); return; }
  let tries = 0;
  const tick = setInterval(async () => {
    tries++;
    let j = null, restarting = false;
    try { j = await api('GET', '/api/jobs/' + encodeURIComponent(jobId)); }
    catch (_e) { restarting = true; }            // server bouncing between accept + response
    if (j && j.error && !j.status) restarting = true;  // job forgotten → already restarted
    if (j && j.status === 'failed') {
      clearInterval(tick); _releaseActionBusy = false;
      _renderPromoteOutcome(j, `<strong>Rollback failed:</strong> `
        + `${escHtml(j.error || 'see the step log below')}`, 'var(--red)');
      return;
    }
    if ((j && j.status === 'complete') || restarting) {
      clearInterval(tick); _releaseActionBusy = false; _confirmPromoted(done); return;
    }
    if (tries > 120) { clearInterval(tick); _releaseActionBusy = false; _confirmPromoted(done); }  // ~4min safety stop
  }, 2000);
}

// Freeze / resume auto-promotion at the current release. POSTs to
// /api/release/pin {pinned}; just rewrites release.json (no redeploy, no
// restart), so we toast + refresh the panel in place.
async function togglePinRelease(pin) {
  if (_releaseActionBusy) return;
  const msg = pin
    ? 'Pause automatic updates?\n\nThe fleet stays exactly '
      + 'where it is — an update in testing will not go live until you resume.'
    : 'Resume automatic updates?\n\nAn update that passes testing will '
      + 'go live to the fleet automatically again.';
  if (!await confirmModal(msg)) return;
  _releaseActionBusy = true;
  api('POST', '/api/release/pin', { pinned: !!pin })
    .then(resp => {
      if (resp && resp.error) { toast(resp.error, 'warn'); return; }
      toast(pin ? 'Automatic updates paused.' : 'Automatic updates resumed.', 'ok');
      _releaseStatus = null;            // force a fresh snapshot on the next render
      return refreshReleaseStatus();
    })
    .catch(err => toast('Couldn\'t change auto-updates: ' + String(err), 'warn'))
    .finally(() => { _releaseActionBusy = false; });
}

// ── Consolidated "Updates" summary cell + slim loud banner ─────────────────
// The Overview used to stack three blocks between the PODS header and the bot
// grid — the lag banner, the RELEASE PIPELINE panel, and the OpenClaw upgrade
// banner — pushing the bots (the dashboard's primary purpose) below the fold.
// They now collapse into a 5th UPDATES summary-stat cell that shows the single
// highest-priority state as a compact token, plus a collapsed detail region
// (#ov-updates-detail) holding the FULL content as ONE consolidated card
// (renderUpdatesDrawer → #ov-release-panel; #ov-sync-banner is reserved for live
// promote/rollback job progress). Steady state costs zero rows above the grid;
// only genuine urgent states surface a slim one-line banner (#ov-loud-banner).
//
// _ovOcData is the async /api/oc/version snapshot, handed in by loadTrust()
// (main script) via noteOcVersion() — the cell needs it to fold "OpenClaw
// update ready" into its priority order, and the drawer card renders its row.
let _ovOcData = null;

function noteOcVersion(ov) {
  _ovOcData = ov || null;
  renderUpdatesDrawer();   // OC is a drawer-card row; re-render when it lands
  _refreshUpdatesCell();
}

// The §9.13 chevron SVG, appended to the slim loud banner as an "expand" hint.
function _updatesCaretSvg() {
  return `<span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"></polyline></svg></span>`;
}

// Acknowledge suppresses the loud lag-stuck BANNER only (the cell keeps telling
// the truth). Keyed by stable version so a new release's lag re-surfaces — same
// semantics as the existing evolve.releaseAck.canary.<stableVersion> key written
// by ackCanaryLag(). (The evolve.releaseSnooze.<tier> key is still honored by the
// in-region direct banner; under D-8 no loud state is snoozable — only security
// is loud in direct mode and it never snoozes — so the cell doesn't consult it.)
function _isCanaryLagAcked(stableV) {
  if (!stableV) return false;
  try { return localStorage.getItem(`evolve.releaseAck.canary.${stableV}`) === stableV; }
  catch (_e) { return false; }
}

// Residual-soak text for the soaking cell sub-label. Prefers the git-backed
// snapshot's server-computed remaining minutes; falls back to soak_started_at +
// soak_minutes; else a generic label.
function _soakLeftText(rel, relStatus, cand) {
  if (relStatus && typeof relStatus.soak_minutes_remaining === 'number') {
    return relStatus.soak_minutes_remaining <= 0 ? 'finishing tests' : `~${relStatus.soak_minutes_remaining}m left`;
  }
  if (cand && cand.soak_started_at && rel && rel.soak_minutes) {
    const started = Date.parse(cand.soak_started_at);
    if (!isNaN(started)) {
      const mins = Math.max(0, Math.round((started + rel.soak_minutes * 60000 - Date.now()) / 60000));
      return mins <= 0 ? 'finishing tests' : `~${mins}m left`;
    }
  }
  return 'testing';
}

// Derive the single highest-priority "updates" state per the deploy-owned D-8
// state→representation contract (docs/spec-deploy-meta-2026-06-14.md §D-8 +
// docs/fixtures/deploy-updates-cell-states.json). deploy owns the truth (which
// state, severity, banner); this implements the reference updatesCellState()
// derivation 1:1. The one rule: a state surfaces the slim above-the-grid banner
// iff severity === 'loud' AND it is not snoozed/acked; the cell always renders.
//
// Graceful degradation while the deploy-side data-contract deltas (★) are
// in flight: no `pin` → not pinned; no `stable.promoted_at` → lag reads STUCK
// (fail-loud, never fail-silent); `corrupt` is still detectable via the
// git-backed /api/release/status CORRUPT state.
function _classifyUpdates() {
  const sd = _statusData || {};
  const bots = sd.bots || {};
  const rel = sd.release || {};
  const mode = rel.mode || 'direct';
  const relStatus = _releaseStatus || null;     // git-backed snapshot (canary)
  const oc = _ovOcData || null;
  const latest = sd.latest_release || null;     // direct-mode tiered release

  const lag = lagState(bots, rel);
  const corrupt = rel.corrupt === true || !!(relStatus && relStatus.state === 'CORRUPT');
  const pin = rel.pin || (relStatus && relStatus.pin) || null;
  const cand = rel.candidate || (relStatus && relStatus.candidate) || null;
  const candState = (cand && cand.state) || null;
  const stableV = lag.stableV || sd.evolve_current_version || null;
  const promoted = (rel.stable && rel.stable.promoted_at)
    || (relStatus && relStatus.stable && relStatus.stable.promoted_at) || null;

  // behind/nBehind/total come from the shared lagState selector (members exclude
  // the primary and the canary/soak bot, which runs ahead by design). Under
  // canary, evolve_synced is recomputed server-side vs the stable pointer.
  const { behind, nBehind, total } = lag;

  const GRACE = 30 * 60 * 1000;   // 2 puller ticks — see D-8 REDEPLOY_GRACE
  const promotedMs = promoted ? Date.parse(promoted) : NaN;
  const sincePromote = isNaN(promotedMs) ? Infinity : (Date.now() - promotedMs);
  // lag-stuck / lag-redeploying are CANARY-pipeline concepts (a promoted pointer
  // the fleet should be on). In direct mode, bots "behind" means behind the
  // available tiered release → update-security / update-available, never lag.
  const isCanary = mode === 'canary';
  const transient = isCanary && nBehind > 0 && promoted && !isNaN(promotedMs) && sincePromote <= GRACE;
  const stuck = isCanary && nBehind > 0 && (!promoted || isNaN(promotedMs) || sincePromote > GRACE);
  const pinned = !!pin && !corrupt;
  const ocReady = !!(oc && oc.update_available);
  const verLabel = stableV ? verDateLabel(stableV) : '';
  const sBots = total === 1 ? '' : 's';

  // The expand region carries detail whenever there's a pipeline (canary), an
  // OC update, lag, an in-flight/failed candidate, a pin, or corruption.
  const hasDetail = mode === 'canary' || ocReady || nBehind > 0 || !!cand || pinned || corrupt;
  const base = { behind, total, stableV, hasDetail, loud: false };
  const quiet = (key, cellText, cellSub, cellColor) =>
    Object.assign({}, base, { key, cellText, cellSub, cellColor });
  const RED_BG = 'rgba(255,107,107,0.12)', RED_BORDER = 'rgba(255,107,107,0.45)';
  const loud = (key, cellText, cellSub, loudHtml, suppressed) => Object.assign({}, base, {
    key, cellText, cellSub, cellColor: 'var(--red)',
    loud: !suppressed, loudColor: 'var(--red)', loudBg: RED_BG, loudBorder: RED_BORDER, loudHtml,
  });

  // ── rank 1 — pipeline-halted (corrupt release.json freezes promotion) ──
  if (corrupt) {
    return loud('pipeline-halted', 'Updates halted', 'needs attention',
      `<span style="flex:1"><strong>⚠ Updates halted — needs attention</strong> — the update config is unreadable, so updates are paused.</span>${_updatesCaretSvg()}`,
      false);
  }
  // ── rank 2 — lag-stuck (update genuinely failed; ack suppresses banner only) ──
  if (stuck) {
    const acked = _isCanaryLagAcked(stableV);
    return loud('lag-stuck', 'Update failed', `${nBehind} of ${total} stuck`,
      `<span style="flex:1"><strong>⚠ Update failed on ${nBehind} of ${total} bot${sBots}${verLabel ? ' (' + escHtml(verLabel) + ')' : ''}</strong> — open the detail to roll back or investigate.</span>${_updatesCaretSvg()}`,
      acked);
  }
  // ── rank 3 — update-security (direct mode; never snoozes) ──
  if (mode !== 'canary' && latest && latest.tier === 'security' && nBehind) {
    const cta = `<button class="btn btn-sm" style="white-space:nowrap;background:var(--red);color:#fff;border:none" onclick="event.stopPropagation();sysmUpgrade(null)">🛡 Apply now</button>`;
    return loud('update-security', 'Security update', `${nBehind} waiting`,
      `<span style="flex:1"><strong>🛡 Security update — apply now</strong> · ${nBehind} of ${total} bot${sBots} waiting to update</span>${cta}${_updatesCaretSvg()}`,
      false);
  }
  // ── rank 4 — oc-update (OpenClaw runtime axis, independent of the pipeline) ──
  if (ocReady) {
    const sc = oc.safety_check;
    const ocSub = !sc ? 'check'
      : sc.running ? 'checking…'
      : sc.ok ? '✅ safe' : '❌ unsafe';
    return quiet('oc-update', 'OpenClaw update', ocSub, 'var(--yellow)');
  }
  // ── rank 5 — pin-held (auto-updates paused; outranks testing — a paused
  //             update will NOT go live, so never imply it will) ──
  if (pinned) {
    return quiet('pin-held', 'Auto-updates paused',
      (pin && pin.reason) ? String(pin.reason) : 'manually paused', 'var(--text3)');
  }
  // ── rank 6 — candidate-soaking ──
  if (candState === 'soaking') {
    return quiet('candidate-soaking', 'Testing update…', _soakLeftText(rel, relStatus, cand), 'var(--purple)');
  }
  // ── rank 7 — candidate-checking (pre-test checks) ──
  if (candState === 'checking') {
    return quiet('candidate-checking', 'Checking update…', 'running checks', 'var(--purple)');
  }
  // ── rank 8 — candidate-blocked (failed pre-test checks; transient amber,
  //             escalates to loud lag-stuck via the pointer once it persists) ──
  if (candState === 'failed') {
    return quiet('candidate-blocked', 'Update blocked', 'failed checks', 'var(--yellow)');
  }
  // ── rank 9 — lag-redeploying (the D-3 fold: expected post-update window) ──
  if (transient) {
    return quiet('lag-redeploying', 'Updating…', `${nBehind} of ${total}`, 'var(--text2)');
  }
  // ── rank 10 — update-available (direct mode, feature/maintenance tier) ──
  if (mode !== 'canary' && latest && nBehind) {
    return quiet('update-available', 'Update available', `${nBehind} waiting`, 'var(--yellow)');
  }
  // ── rank 10.5 — direct-mode auto-updating (the reconcile fix) ──
  // Bots are behind the current code but `latest` didn't resolve a curated
  // release entry — which happens precisely in the non-monotonic case (the tip
  // PR# is LOWER than what's deployed, so release_notes' lexical range is
  // empty). Without this rank the cell fell through to "Up to date" while the
  // Maintenance System badge — reading the same identity-based `evolve_synced`
  // — showed the bot behind: the two surfaces contradicted. In direct mode the
  // repo-puller auto-redeploys lagging bots, so show the same calm "Updating…"
  // as the canary transient (rank 9), no loud alert, no manual upgrade.
  if (mode !== 'canary' && nBehind) {
    return quiet('lag-redeploying', 'Updating…', `${nBehind} of ${total}`, 'var(--text2)');
  }
  // ── rank 11 — up-to-date ──
  return quiet('up-to-date', 'Up to date', verLabel ? `on ${verLabel}` : 'fleet current', 'var(--green)');
}

// Re-render the cell + slim banner + region empty-state from current state.
// Cheap (no tile re-render); called from renderOverview() and whenever an
// async source (OC version, git-backed release status) lands.
function _refreshUpdatesCell() {
  if (!_statusData) return;
  // Keep the cell chevron in sync with the region no matter which affordance
  // toggles it (summary header, cell, or a persisted-open state on load).
  // Wired here (runs on first render) so it's live before any user click.
  const region0 = document.getElementById('ov-updates-detail');
  if (region0 && !region0.dataset.caretWired) {
    region0.dataset.caretWired = '1';
    region0.addEventListener('toggle', _syncUpdatesCaret);
  }
  const state = _classifyUpdates();
  const token = document.getElementById('ov-updates-token');
  const sub = document.getElementById('ov-updates-sub');
  if (token) { token.textContent = state.cellText; token.style.color = state.cellColor; }
  if (sub) sub.textContent = state.cellSub;
  _syncUpdatesCaret();

  const loud = document.getElementById('ov-loud-banner');
  if (loud) {
    if (state.loud) {
      loud.style.display = 'flex';
      loud.style.background = state.loudBg;
      loud.style.border = `1px solid ${state.loudBorder}`;
      loud.style.color = state.loudColor;
      loud.innerHTML = state.loudHtml;
    } else {
      loud.style.display = 'none';
      loud.innerHTML = '';
    }
  }

  // Empty-state line so an operator who expands the region in a fully-clear
  // state (direct mode, fleet current, no OC update) sees a positive
  // confirmation rather than a blank box.
  const empty = document.getElementById('ov-updates-empty');
  if (empty) {
    if (state.hasDetail) { empty.style.display = 'none'; }
    else {
      empty.style.display = 'block';
      empty.textContent = state.stableV
        ? `Everything's up to date — fleet on ${verDateLabel(state.stableV)}. No update in flight.`
        : `Everything's up to date. No update in flight.`;
    }
  }
}

// Mirror the detail region's open state onto the summary cell: rotate the
// chevron AND set the cell's .is-active state so it reads as "pressed" and
// visually owns the panel that opens directly below the bar (CSS:
// #ov-updates-segment.is-active + .updates-detail[open] accent tether). This is
// the single chokepoint every open-path routes through (the cell click via
// toggleUpdatesDetail, the loud-banner force-open, _openUpdatesRegion, and the
// region's own native `toggle` event listener), so the active-state can never
// desync from the region's open-state.
function _syncUpdatesCaret() {
  const region = document.getElementById('ov-updates-detail');
  const open = !!(region && region.open);
  const caret = document.getElementById('ov-updates-caret');
  if (region && caret) caret.classList.toggle('is-open', open);
  const seg = document.getElementById('ov-updates-segment');
  if (seg) seg.classList.toggle('is-active', open);
}

// Toggle the detail region (force=true/false to set explicitly). Setting
// region.open fires the native `toggle` event, which collapsible.js persists
// to localStorage; we only sync the cell chevron here. Wired once for the
// summary's own toggle (keyboard / click on the in-region header) so the cell
// chevron stays in sync no matter which affordance the operator uses.
function toggleUpdatesDetail(force) {
  const region = document.getElementById('ov-updates-detail');
  if (!region) return;
  region.open = (force === true) ? true : (force === false ? false : !region.open);
  _syncUpdatesCaret();   // toggle-event listener (wired in _refreshUpdatesCell) also fires
}

// Inline-onclick handlers must be window-exported so the ESLint
// suppressions-baseline gate sees them as used (the baseline only shrinks).
window.ackCanaryLag = ackCanaryLag;
window.promoteRelease = promoteRelease;
window.renderUpdatesDrawer = renderUpdatesDrawer;
window.refreshReleaseStatus = refreshReleaseStatus;
window.rollbackRelease = rollbackRelease;
window.togglePinRelease = togglePinRelease;
window.toggleUpdatesDetail = toggleUpdatesDetail;
window.noteOcVersion = noteOcVersion;

function renderFunnel(containerId, f) {
  if (!f) return;
  const stages = [
    { key:'pending', label:'Generated' },
    { key:'reviewed', label:'Reviewed' },
    { key:'approved', label:'Approved' },
    { key:'deployed', label:'Applied' },
    { key:'positive_outcomes', label:'Positive' },
  ];
  const max = Math.max(...stages.map(s => f[s.key] || 0), 1);
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = `
    <div class="funnel" style="align-items:flex-end;padding:0 8px">${stages.map(s => {
      const v = f[s.key] || 0;
      const h = Math.max(10, Math.round((v / max) * 100));
      return `<div class="funnel-bar" style="height:${h}%"><span class="funnel-val">${v}</span></div>`;
    }).join('')}</div>
    <div class="funnel-labels">${stages.map(s => `<div class="funnel-label">${s.label}</div>`).join('')}</div>`;
}


// ══════════════════════════════════════════════════════
// Bot lifecycle actions (Overview)
// ══════════════════════════════════════════════════════

// Tile window toggle (7d / 28d) — persists in localStorage so the user's
// choice survives reloads. The renderer reads window._tileWindow.
// "30d" is migrated to "28d" on read so users with the old value stored
// don't get stuck on a window the renderer no longer supports.
window._tileWindow = (() => {
  try {
    const saved = localStorage.getItem('evolveTileWindow');
    if (saved === '7d' || saved === '28d') return saved;
    return '7d';
  } catch { return '7d'; }
})();
function setTileWindow(w) {
  if (w !== '7d' && w !== '28d') return;
  window._tileWindow = w;
  try { localStorage.setItem('evolveTileWindow', w); } catch { /* ignore */ }
  document.querySelectorAll('#pod-window-toggle button').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.window === w);
  });
  if (typeof renderOverview === 'function' && _statusData) renderOverview();
}

// Tile chip navigation. Four target shapes:
//   chipNav("security")               → navigate to Security page
//   chipNav("maintenance/system")     → navigate to Maintenance, then activate
//                                       the System subtab via the existing
//                                       subTab() helper.
//   chipNav("settings/bots/<bot_id>") → navigate to Settings → Bots, then
//                                       select the named bot via
//                                       switchConfigBot(). This is the
//                                       3-level shape that the chip
//                                       explainer popover uses for the
//                                       "Open this bot's settings"
//                                       remediation. Other (page, subtab)
//                                       pairs can register a similar
//                                       identifier handler below when
//                                       they need deep-linking to a
//                                       specific row.
//   chipNav("pair:<bot>:<channel>")   → open the pairing wizard modal for
//                                       that bot+channel. Used by the
//                                       "🔗 Pair messaging ID" chip rule
//                                       in tile_metrics → pairing_chip.
//
// The subtab activation uses [onclick*="'<id>'"] to match the subtab's
// inline handler — same pattern as the existing infrajobs nav at L11083.
function chipNav(target) {
  if (!target) return;
  // Pairing-modal target (action prefix; not a page route).
  if (target.indexOf('pair:') === 0) {
    const parts = String(target).split(':');
    const botId = parts[1] || '';
    const channel = parts[2] || '';
    if (botId) openPairingWizard(botId, channel);
    return;
  }
  let [page, subtab, identifier] = String(target).split('/');
  // Resolve legacy page slugs via pageRedirectRegistry — the registry
  // already powers nav() for legacy data-page attributes, but chipNav
  // looks up the [data-page=...] element directly. Without this step
  // chips that carry legacy nav targets (e.g. "capabilities" — the
  // old slug for Apps) silently fail because no DOM element matches.
  // Explicit subtabs in the target win over the redirect's default.
  if (typeof pageRedirectRegistry === 'object' && pageRedirectRegistry[page]) {
    const redirect = pageRedirectRegistry[page];
    page = redirect.page;
    if (!subtab && redirect.subtab) subtab = redirect.subtab;
  }
  const pageEl = document.querySelector(`[data-page="${page}"]`);
  if (!pageEl) return;
  nav(pageEl);
  if (!subtab) return;
  setTimeout(() => {
    const tabEl = document.querySelector(
      `#page-${page} [onclick*="'${subtab}'"]`
    );
    if (tabEl) subTab(tabEl, page, subtab);
    // 3-level: page/subtab/identifier — the identifier is a context
    // selector specific to that (page, subtab) pair. Register new
    // selectors here as deep-link patterns get added. Defer one tick
    // after subTab so the target view has mounted (the bot tab strip
    // inside #settings-bots is rendered by initConfigBotSelector
    // which runs from the subTab activation handler).
    if (identifier && page === 'settings' && subtab === 'bots') {
      setTimeout(() => {
        if (typeof switchConfigBot === 'function') {
          try { switchConfigBot(identifier); } catch (e) {
            console.error('chipNav: switchConfigBot failed', e);
          }
        }
      }, 50);
    }
  }, 80);
}

// Chip explainer popover. Per docs/principle-alerts-explain-and-remediate.md,
// chips that carry the `why` field open this popover on click instead of
// navigating directly. The popover renders label / why / impact / trigger
// (the existing `detail` string) / remediations[]. Remediation buttons
// reuse chipNav() so deep-links land at the right page+subtab. The chip
// data is read from window._tileChipExplainers, which the render code
// populates per (bot_id, chip_id).
function openChipExplainer(botId, chipId) {
  const reg = window._tileChipExplainers || {};
  const c = (reg[botId] || {})[chipId];
  if (!c) return;
  const titleEl = document.getElementById('chip-explainer-title');
  const bodyEl = document.getElementById('chip-explainer-body');
  if (!titleEl || !bodyEl) return;
  const sevLabel = c.severity === 'critical' ? 'Critical'
    : c.severity === 'warn' ? 'Warning'
    : c.severity === 'ok' ? 'OK'
    : c.severity === 'info' ? 'Info'
    : c.severity || '';
  titleEl.innerHTML = `${escHtml(c.label || chipId)} <span style="font-size:0.7rem;color:var(--text3);font-weight:400;margin-left:6px">${escHtml(sevLabel)} · ${escHtml(botId)}</span>`;
  let html = '';
  if (c.why) html += `<p style="margin:0 0 10px 0">${escHtml(c.why)}</p>`;
  if (c.impact) {
    html += `<div style="margin-bottom:8px"><span style="font-size:0.7rem;text-transform:uppercase;letter-spacing:0.06em;color:var(--text3);margin-right:6px">Impact</span><span style="color:var(--text2)">${escHtml(c.impact)}</span></div>`;
  }
  if (c.detail) {
    html += `<div style="margin-bottom:8px"><span style="font-size:0.7rem;text-transform:uppercase;letter-spacing:0.06em;color:var(--text3);margin-right:6px">Trigger</span><span style="color:var(--text2)">${escHtml(c.detail)}</span></div>`;
  }
  const rems = Array.isArray(c.remediations) ? c.remediations : [];
  if (rems.length > 0) {
    html += `<div style="margin-top:14px;font-size:0.7rem;text-transform:uppercase;letter-spacing:0.06em;color:var(--text3);margin-bottom:6px">What to do</div>`;
    html += `<div style="display:flex;flex-direction:column;gap:6px">`;
    rems.forEach(r => {
      const label = escHtml(r.label || 'Open');
      if (r.kind === 'action' && r.action) {
        // Action remediation — fire a registered chip-explainer
        // action (e.g. "run a scan now") with the bot_id as context.
        // Registered actions live in _CHIP_EXPLAINER_ACTIONS below.
        // Different visual treatment from deep-links so the operator
        // can see "this will DO something" vs "this will TAKE me
        // somewhere".
        const action = escHtml(r.action);
        html += `<button class="btn btn-primary" style="text-align:left;justify-content:flex-start" onclick="closeChipExplainer();_chipExplainerAction('${action}','${escHtml(botId)}')">${label}</button>`;
      } else if (r.nav) {
        const nav = escHtml(r.nav);
        html += `<button class="btn btn-ghost" style="text-align:left;justify-content:flex-start" onclick="closeChipExplainer();chipNav('${nav}')">${label} →</button>`;
      } else {
        html += `<div style="font-size:0.8rem;color:var(--text2);padding:4px 0">${label}</div>`;
      }
    });
    html += `</div>`;
  }
  if (c.remediation_note) {
    html += `<p style="margin-top:14px;font-size:0.74rem;color:var(--text3);font-style:italic;line-height:1.5">${escHtml(c.remediation_note)}</p>`;
  }
  bodyEl.innerHTML = html;
  const overlay = document.getElementById('chip-explainer-modal');
  if (overlay) overlay.classList.add('open');
}

function closeChipExplainer() {
  const overlay = document.getElementById('chip-explainer-modal');
  if (overlay) overlay.classList.remove('open');
}

// Registry of in-popover actions. A chip's remediation can carry
// ``kind: 'action', action: 'foo'`` and the popover will render a
// primary-styled button that fires _CHIP_EXPLAINER_ACTIONS.foo(bot_id)
// after closing the modal. Keeps the action surface explicit (versus
// arbitrary inline onclick) so the registry doubles as the inventory
// of "what can a chip popover make happen."
const _CHIP_EXPLAINER_ACTIONS = {
  // Fire an app scan for one bot via the same endpoint the Apps page
  // uses (POST /api/applications/scan?bot=<id>). Fire-and-forget +
  // toast — the operator can navigate to Apps to watch progress, but
  // the action itself doesn't depend on that page being mounted.
  'run_capability_scan': (botId) => {
    if (!botId) return;
    toast(`Starting scan for ${botLabel(botId)}…`, 'ok');
    api('POST', `/api/applications/scan?bot=${encodeURIComponent(botId)}`)
      .then(r => {
        if (!r) { toast('✗ Scan failed to start', 'err'); return; }
        if (r.error) { toast('✗ ' + r.error, 'err'); return; }
        if (r.status === 'already_running') {
          toast(`Scan already in progress for ${botLabel(botId)}`, 'warn');
          return;
        }
        toast(`✓ Scan started for ${botLabel(botId)} — open Apps to watch progress`, 'ok');
      })
      .catch(_e => toast('✗ Scan failed to start', 'err'));
  },
};

function _chipExplainerAction(action, botId) {
  const fn = _CHIP_EXPLAINER_ACTIONS[action];
  if (typeof fn !== 'function') {
    console.error('chip-explainer: unknown action', action);
    return;
  }
  try { fn(botId); }
  catch (e) { console.error('chip-explainer action failed', e); }
}

// In-page scroll companion to chipNav — for chips on the Cost Measures
// page that link to a section on the same page rather than another page.
function chipScroll(selector) {
  const el = typeof selector === 'string'
    ? document.querySelector(selector)
    : selector;
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// Usage page Turns/Cost toggle — governs both timeline charts. Persisted
// in localStorage so a refresh keeps the operator's choice.
window._usageUnit = (() => {
  try { return localStorage.getItem('evolveUsageUnit') || 'turns'; }
  catch { return 'turns'; }
})();
function setUsageUnit(u) {
  if (u !== 'turns' && u !== 'cost') return;
  window._usageUnit = u;
  try { localStorage.setItem('evolveUsageUnit', u); } catch { /* ignore */ }
  document.querySelectorAll('#usage-unit-toggle button').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.unit === u);
  });
  // Re-render the two timelines AND the composition bar with the same cached
  // payload — the metric (Turns | Cost) reweights the composition bar widths,
  // bold %, and legend sort, not just the timelines.
  if (window._lastUsageData) {
    _renderUsageChart(window._lastUsageData);
    _renderUsageTriggerChart(window._lastUsageData);
    if (typeof _renderUsageComposition === 'function') {
      _renderUsageComposition(window._lastUsageData);
    }
  }
}

async function redeployBot(id, role) {
  const r = await api('POST', '/api/deploy', { botId: id, role });
  toast(r.ok ? `✓ ${id} redeployed` : `✗ ${r.error||'Failed'}`, r.ok ? 'ok' : 'err');
  if (r.ok) await loadStatus();
}

// ── Lifecycle removal: detach / retire / delete ─────────────────────────────
// Three first-class paths. The old `removeBot` called /api/remove which
// silently left 7+ daemons running (gateway, apply, test, cost-converter,
// audit-runner ×2, doctor-pass, backup) and never touched /Users/<bot>/.
// All three new endpoints accept { botId, dryRun?, confirmation? } and
// return RetireResult-shape responses (success, steps, plists_stopped,
// plists_failed, archive_path).

// Shared toast + always-refresh handler for lifecycle responses.
//
// The RetireResult `success` flag is too binary: retire_bot sets it
// false on ANY error (including non-fatal cases like "one plist was
// already booted-out" or "notification dispatcher couldn't reach
// Telegram"). The bot can be effectively removed from network.json
// AND archived AND have its daemons stopped, but `success` is false
// because one non-blocking step had a hiccup.
//
// Previously this code:
//   * Showed bare "Failed" with no detail (the response has
//     `errors: [...]` array, not `error: "..."` string, so the
//     `r.error || 'Failed'` fallthrough always rendered "Failed")
//   * Gated `loadStatus()` on r.success — so when retire/delete
//     partially succeeded, the bot was gone from network.json but
//     the tile stayed visible until the next polling tick (~5-10s)
//
// The fix: surface the real error detail from `errors[]`, distinguish
// partial-success from full-failure (archive_path present + some
// plists stopped → partial), and ALWAYS call loadStatus so the UI
// reflects ground truth regardless of the success-flag verdict.
async function _lifecycleHandle(verb, id, r) {
  const stopped = (r.plists_stopped && r.plists_stopped.length) || 0;
  const failed = (r.plists_failed && r.plists_failed.length) || 0;
  // RetireResult.errors[] commonly has multiple entries (one per
  // failed sub-step). Surfacing only errors[0] hid which sub-step
  // was actually wedged. Warn toasts are pinned + pre-wrap so the
  // joined detail stays on screen until the operator dismisses.
  const errList = (r.errors && r.errors.length) ? r.errors
                : r.error ? [r.error]
                : ['Failed (see admin log for details)'];
  if (r.success) {
    const archiveTrailer = r.archive_path ? `, archive: ${r.archive_path}` : '';
    toast(`✓ ${id} ${verb} (${stopped} daemons stopped${archiveTrailer})`, 'ok');
  } else if (r.archive_path || stopped > 0) {
    // Partial success: the bot was demonstrably acted on. Operator
    // needs to know both "the action happened" and "something is
    // worth checking in the logs."
    toast(
      `⚠ ${id} partially ${verb} (${stopped} stopped, ${failed} failed):\n` +
      errList.join('\n'),
      'warn',
    );
  } else {
    toast(`✗ ${id} ${verb} failed: ${errList[0]}`, 'err');
  }
  // Always refresh — even on full failure, the UI should reflect
  // whatever partial state landed (a half-completed retire might
  // have stopped some daemons; the operator needs to see that).
  try { await loadStatus(); } catch (_e) { /* don't mask the toast */ }
}

async function detachBot(id) {
  if (!await confirmModal({
    body: (
      `Detach ${id} from Evolve?\n\n` +
      `• Stops the per-bot Evolve daemons (apply, test, cost-converter, ` +
      `audit-runner ×2, doctor-pass, backup)\n` +
      `• Strips the evolve plugin from the bot's openclaw.json\n` +
      `• The bot keeps running as an OpenClaw bot — gateway stays up\n` +
      `• Reversible: a re-deploy puts the plugin back`
    ),
    danger: true,
  })) return;
  const r = await api('POST', '/api/lifecycle/detach', { botId: id });
  await _lifecycleHandle('detached', id, r);
}

async function retireBot(id) {
  if (!await confirmModal({
    body: (
      `Retire ${id}?\n\n` +
      `• Archives bot data to {sharedDir}/retired/${id}/\n` +
      `• Stops every per-bot daemon including the gateway\n` +
      `• Removes from network.json\n` +
      `• macOS user account stays in place\n` +
      `• Reversible: the archive contains everything needed to revive`
    ),
    danger: true,
  })) return;
  const r = await api('POST', '/api/lifecycle/retire', { botId: id });
  await _lifecycleHandle('retired', id, r);
}

function confirmDeleteBot(id) {
  // Populate + open the typed-DELETE modal. We deliberately use a real
  // DOM modal (not a window.prompt) so the warning copy is fully visible
  // alongside the input — typing DELETE in a prompt() without seeing the
  // consequences spelled out is exactly the footgun the gate exists to
  // prevent. Server-side gate at /api/lifecycle/delete also enforces
  // confirmation === "DELETE", so a tampered frontend can't sneak past.
  const modal = document.getElementById('delete-bot-modal');
  const input = document.getElementById('delete-bot-modal-input');
  const errBox = document.getElementById('delete-bot-modal-error');
  if (!modal || !input) {
    // Fallback if the modal isn't on this page for any reason.
    const typed = prompt(`Type DELETE to irreversibly delete ${id}:`, '');
    if (typed && typed.trim() === 'DELETE') _doDeleteBot(id);
    return;
  }
  document.getElementById('delete-bot-modal-id').textContent = id;
  document.getElementById('delete-bot-modal-home').textContent = `/Users/${id}/`;
  input.value = '';
  if (errBox) { errBox.style.display = 'none'; errBox.textContent = ''; }
  modal.classList.add('open');
  setTimeout(() => input.focus(), 0);
}

function closeDeleteBotModal() {
  const modal = document.getElementById('delete-bot-modal');
  if (modal) modal.classList.remove('open');
}

async function _doDeleteBotFromModal() {
  const id = document.getElementById('delete-bot-modal-id').textContent;
  const input = document.getElementById('delete-bot-modal-input');
  const errBox = document.getElementById('delete-bot-modal-error');
  const typed = (input?.value || '').trim();
  if (typed !== 'DELETE') {
    if (errBox) {
      errBox.textContent = 'Type DELETE exactly (case-sensitive) to enable the button.';
      errBox.style.display = 'block';
    }
    return;
  }
  closeDeleteBotModal();
  await _doDeleteBot(id);
}

async function _doDeleteBot(id) {
  const r = await api('POST', '/api/lifecycle/delete', { botId: id, confirmation: 'DELETE' });
  await _lifecycleHandle('deleted', id, r);
}

async function toggleBotMultiUser(id, currentlyMulti) {
  const next = !currentlyMulti;
  const label = next ? 'shared (multi-user)' : 'personal (single-user)';
  if (!await confirmModal(`Mark ${id} as ${label}?\n\nThis affects which security audit warnings are shown.`)) return;
  const r = await api('PATCH', '/api/bot/multi-user', { botId: id, multiUser: next });
  if (r?.ok) {
    toast(`✓ ${id} marked as ${label}`, 'ok');
    await loadStatus();
  } else {
    toast(`✗ ${r?.error || 'Failed'}`, 'err');
  }
}

async function toggleBotContinuityEngine(id, currentlyEnabled) {
  const action = currentlyEnabled ? 'disable' : 'enable';
  const r = await api('POST', `/api/bots/${encodeURIComponent(id)}/continuity-engine/${action}`);
  if (r?.ok) {
    toast(`✓ ${id} CE ${currentlyEnabled ? 'disabled' : 'enabled'}`, 'ok');
    await loadStatus();
  } else {
    toast(`✗ ${r?.error || 'Failed'}`, 'err');
  }
}

async function restartGatewayFromOverview(id) {
  const r = await api('POST', `/api/admin/gateway/${encodeURIComponent(id)}/restart`, { confirm: true });
  if (r && !r.error) {
    toast(`✓ Gateway restarted for ${id}`, 'ok');
    setTimeout(loadStatus, 3000);
  } else {
    toast(`✗ ${r?.error || 'Restart failed'}`, 'err');
  }
}


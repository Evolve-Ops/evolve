// ════════════════════════════════════════════════════════════════════════
// Page subtab: Maintenance — Gateway + Cron + Infra Jobs + OC Version +
//                            Safety + Logs + Analytics Turns
//
// The Gateway-tier surfaces on Maintenance page. Cached gateway status
// is exposed as a module global (_gwStatusCache) and consumed by the
// Plugins page's ik-row renderer at runtime (free-variable lookup).
//
// Sub-clusters:
//
//   Module-level cache + _gwCachedErrors helper (top)
//   Gateway          — loadGateway + gatewayRestartBot
//   Cron Jobs        — loadGatewayCron (Gateway sub-tab)
//   Infra Jobs       — loadInfraJobs (launchctl-driven services)
//   OC Version       — loadOcVersion + renderOcUpgradeBanner (data now
//                      lives in the merged Bot Versions table on the
//                      Maintenance → System sub-tab; the standalone OC
//                      Version tab was retired)
//   Safety report    — runSafetyCheck + pollSafetyCheck + openSafetyReport
//                      + closeSafetyReport + renderSafetyReportInto +
//                      _gateSummary helper
//   Gateway Log Viewer — toggleLogsAutoRefresh + loadLogsForBot +
//                        loadGatewayLogs
//   Analytics Turns  — loadAnalyticsTurns + loadTurnsRaw + _fetchTurnsRaw
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - api(), toast(), escHtml(), botLabel() — core/ (plus the inline
//     botLabel that's still in the main script just below this cluster)
//   - loadStatus() (Overview) — refreshed after gateway restart
//   - _connState — still inline (loadAnalyticsTurns checks it)
//   - ik-row renderer — uses _gwStatusCache via free-variable lookup
//
// Out of scope (leave inline):
//   loadConfigBot (~line 23493) / loadConfigHealth (~line 26044) /
//   loadInfraAudit (~line 29583) — Maintenance subtabs scattered far
//   below this cluster; future phase.
// ════════════════════════════════════════════════════════════════════════

// Module-level gateway status cache (populated by loadGateway, read by ikRenderKeys)
let _gwStatusCache = null;
let _gwStatusCacheAt = 0;
const _GW_CACHE_TTL = 120_000; // 2 minutes

function _gwCachedErrors(botId) {
  if (!_gwStatusCache || Date.now() - _gwStatusCacheAt > _GW_CACHE_TTL) return [];
  return (_gwStatusCache[botId] || {}).recent_errors || [];
}

// ══════════════════════════════════════════════════════
// Gateway
// ══════════════════════════════════════════════════════
let _gwAutoRefreshTimer = null;

async function loadDeferRunnerHealth() {
  // Defer-runner one-line health for the Maintenance > Status tab.
  // Three numbers + a freshness — matches the "stats not live activity"
  // shape we landed on when the old Continuity tab was deleted.
  const el = document.getElementById('defer-runner-health');
  if (!el) return;
  try {
    const d = await api('GET', '/api/defer/health');
    if (!d || d.error) {
      el.innerHTML = `<span style="color:var(--text3)">Defer runner: status unavailable</span>`;
      return;
    }
    const loadedTag = d.runner_loaded
      ? `<span style="color:var(--green)">●</span> loaded`
      : `<span style="color:var(--red)">●</span> NOT loaded`;
    const lastFire = (d.minutes_since_last_fire == null)
      ? `never`
      : `${d.minutes_since_last_fire} min ago`;
    const todayBits = [];
    if (d.fired_today)  todayBits.push(`<span style="color:var(--green)">${d.fired_today}</span> fired`);
    if (d.failed_today) todayBits.push(`<span style="color:var(--yellow)">${d.failed_today}</span> failed`);
    const today = todayBits.length ? todayBits.join(', ') : 'no fires';
    el.innerHTML = `Defer runner: ${loadedTag} · last fire ${lastFire} · today ${today}`;
  } catch (e) {
    el.innerHTML = `<span style="color:var(--text3)">Defer runner: error fetching status</span>`;
  }
}

async function loadGateway() {
  loadRelatedProposalsStrip('substrate_health');
  loadDeferRunnerHealth();   // fire-and-forget, no await — independent surface
  document.getElementById('gw-status-table').innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  const d = await api('GET', '/api/gateway/status');
  _gwStatusCache = d || {};
  _gwStatusCacheAt = Date.now();
  // Primary first, then alpha-by-label — same shape as every other bot list.
  const bots = orderedBotIds(d).map(id => [id, d[id]]);

  if (!bots.length) {
    document.getElementById('gw-status-table').innerHTML = '<div class="empty">No bots configured.</div>';
    return;
  }

  document.getElementById('gw-status-table').innerHTML = `<div class="card"><div class="resp-table-wrap"><table class="resp-table">
    <thead><tr><th>Bot</th><th>Running</th><th>Reachable</th><th>PID</th><th>Updated</th><th>Status</th><th>Recent Errors</th><th>Controls</th></tr></thead>
    <tbody>${bots.map(([id, s]) => {
      const fresh = !s.ts;  // never had a status file written
      // Cross-reference breaker state from _statusData (overlayed by
      // /api/status, Phase 4b). When an L2 breaker is tripped, the
      // bot is intentionally offline — show "halted" instead of the
      // cached probe data, which may still claim "ok" until heal.py
      // re-runs (every 5 min).
      const _bot = (_statusData?.bots || {})[id] || {};
      const _l2 = (_bot.active_breakers || []).some(b => b.type === 'full')
               || (_statusData?.pod_breakers || []).some(b => b.type === 'full');
      // Reachable (HTTP /evolve/status returned 200) is authoritative — a bot
      // can't answer HTTP without running. The process matcher is a secondary
      // signal that helps distinguish "process stuck" from "process gone" when
      // HTTP fails; never let it override a successful probe.
      const dotClass = _l2 ? 'red'
        : (fresh ? 'gray' : (s.gateway_reachable ? 'green' : (s.gateway_running ? 'yellow' : 'red')));
      const statusBadge = _l2
        ? `<span class="badge" style="background:rgba(248,113,113,0.15);color:var(--red);border:1px solid rgba(248,113,113,0.4)" title="Circuit breaker tripped (full halt). Gateway intentionally stopped. Click Reactivate on the bot tile to bring it back online.">⚡ halted</span>`
        : (fresh
            ? `<span class="badge badge-ok" style="background:rgba(126,184,247,0.15);color:var(--blue)" title="Pending: heal.py hasn't run yet. Normal for new installs — check back in 5 minutes.">Pending</span>`
            : (s.stale ? `<span class="badge badge-warn" title="Stale: status hasn't updated recently. heal.py may have stopped.">stale</span>`
            : (s.gateway_reachable ? `<span class="badge badge-ok">ok</span>`
            : (s.gateway_running ? `<span class="badge badge-warn" title="Process running but not reachable via HTTP probe">degraded</span>`
            : `<span class="badge badge-err">down</span>`))));
      const errNote = buildErrNote(s, fresh);
      // Restart on an L2-halted bot would conflict with bootout state.
      // Swap the Restart button for "Reactivate" that goes through the
      // breaker-reset path instead.
      const controlsCell = _l2
        ? `<button class="btn btn-primary btn-sm" onclick="reactivateBotFromTile('${escHtml(id)}')" title="Bring ${escHtml(botLabel(id))} back online — clears the full-halt breaker." style="background:rgba(61,217,132,0.18);border:1px solid var(--green,#3dd984);color:var(--green,#3dd984)">🔄 Reactivate</button>`
        : `<button class="btn btn-ghost btn-sm" onclick="gatewayRestartBot('${escHtml(id)}')" title="Restart gateway for ${escHtml(botLabel(id))} (requires privileged confirmation)">↺ Restart</button>`;
      // When halted, Running/Reachable cells show the intentional-stop
      // state explicitly, NOT the stale probe data. Less misleading.
      const runningCell = _l2 ? '<span style="color:var(--text3)">⚡ halted</span>'
        : (fresh ? '—' : (s.gateway_running ? '<span style="color:var(--green)">✓ yes</span>' : '<span style="color:var(--red)">✗ no</span>'));
      const reachableCell = _l2 ? '<span style="color:var(--text3)">⚡ halted</span>'
        : (fresh ? '—' : (s.gateway_reachable ? '<span style="color:var(--green)">✓ yes</span>' : '<span style="color:var(--text3)">✗ no</span>'));
      return `<tr>
        <td data-label="Bot"><div style="display:flex;align-items:center;gap:7px"><div class="dot dot-${dotClass}"></div><strong>${escHtml(botLabel(id))}</strong></div></td>
        <td data-label="Running">${runningCell}</td>
        <td data-label="Reachable">${reachableCell}</td>
        <td data-label="PID" style="font-family:monospace;color:var(--text2)">${s.gateway_pid || '—'}</td>
        <td data-label="Updated" style="font-size:0.75rem;color:var(--text2)">${s.ts ? ago(s.ts) : '—'}</td>
        <td data-label="Status">${statusBadge}</td>
        <td data-label="Recent Errors">${errNote}</td>
        <td data-label="Controls">${controlsCell}</td>
      </tr>`;
    }).join('')}</tbody>
  </table></div></div>`;
}

function toggleGwAutoRefresh() {
  if (_gwAutoRefreshTimer) { clearInterval(_gwAutoRefreshTimer); _gwAutoRefreshTimer = null; }
  if (document.getElementById('gw-autorefresh').checked) {
    _gwAutoRefreshTimer = setInterval(loadGateway, 60000);
  }
}

async function gatewayRestartBot(botId) {
  if (!await confirmModal(`Restart the OpenClaw gateway for ${botLabel(botId)}?\n\nThis will briefly interrupt that bot's connections. Confirm to proceed.`)) return;
  const btn = event?.target;
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  const r = await api('POST', `/api/admin/gateway/${encodeURIComponent(botId)}/restart`, { confirm: true });
  if (btn) { btn.disabled = false; btn.textContent = '↺ Restart'; }
  if (r && !r.error) {
    toast(`✓ Gateway restarted for ${botLabel(botId)}`, 'ok');
    setTimeout(loadGateway, 3000);
  } else {
    toast(`✗ ${r?.error || 'Restart failed'}`, 'err');
  }
}

// ══════════════════════════════════════════════════════
// Cron Jobs (Gateway sub-tab)
// ══════════════════════════════════════════════════════
let _cronAutoRefreshTimer = null;
let _cronActiveBotTab = null;

function toggleCronAutoRefresh() {
  if (_cronAutoRefreshTimer) { clearInterval(_cronAutoRefreshTimer); _cronAutoRefreshTimer = null; }
  if (document.getElementById('cron-autorefresh').checked) {
    _cronAutoRefreshTimer = setInterval(loadGatewayCron, 60000);
  }
}

function _cronStatusCell(status) {
  switch (status) {
    case 'ok':       return '<span style="color:var(--green)">✅ OK</span>';
    case 'error':    return '<span style="color:var(--red)">❌ Error</span>';
    case 'warning':  return '<span style="color:var(--yellow)">⚠️ Warning</span>';
    case 'never_run':return '<span style="color:var(--text3)">⏳ Never run</span>';
    default:         return '<span style="color:var(--text3)">—</span>';
  }
}

function _cronExitCell(code) {
  if (code === null || code === undefined) return '<span style="color:var(--text3)">—</span>';
  return code === 0
    ? '<span style="color:var(--green)">✅ 0</span>'
    : `<span style="color:var(--red)">❌ ${escHtml(String(code))}</span>`;
}

function _truncLabel(label, max) {
  return label.length > max ? label.slice(0, max - 1) + '…' : label;
}

function _cronBotTable(jobs) {
  if (!jobs || !jobs.length) return '<div class="empty" style="padding:16px">No jobs found for this bot.</div>';
  const rows = jobs.map(job => {
    const label = _truncLabel(job.label || '—', 35);
    const schedule = job.schedule || '—';
    const lastRun = job.last_run ? ago(job.last_run) : '—';
    const nextRun = job.next_run ? ago(job.next_run) : '—';
    return `<tr>
      <td data-label="Job" style="font-family:monospace;font-size:0.8rem" title="${escHtml(job.label||'')}">${escHtml(label)}</td>
      <td data-label="Schedule" style="font-family:monospace;font-size:0.75rem;color:var(--text2)">${escHtml(schedule)}</td>
      <td data-label="Last Run" style="font-size:0.75rem;color:var(--text2)">${escHtml(lastRun)}</td>
      <td data-label="Exit Code">${_cronExitCell(job.last_exit_code)}</td>
      <td data-label="Next Run" style="font-size:0.75rem;color:var(--text2)">${escHtml(nextRun)}</td>
      <td data-label="Status">${_cronStatusCell(job.status)}</td>
    </tr>`;
  });
  return `<div class="resp-table-wrap"><table class="resp-table">
    <thead><tr>
      <th>Job</th><th>Schedule</th><th>Last Run</th>
      <th>Exit Code</th><th>Next Run</th><th>Status</th>
    </tr></thead>
    <tbody>${rows.join('')}</tbody>
  </table></div>`;
}

function switchCronBotTab(botId, allData) {
  _cronActiveBotTab = botId;
  // Update tab active state
  document.querySelectorAll('.cron-bot-tab').forEach(el => {
    el.classList.toggle('active', el.dataset.bot === botId);
  });
  const panel = document.getElementById('cron-bot-panel');
  if (!panel) return;
  const botData = allData[botId];
  if (!botData) { panel.innerHTML = '<div class="empty">No data.</div>'; return; }
  if (botData.error) {
    const errType = botData.error_type || '';
    const isCliError = errType === 'cli_unavailable' || botData.error.toLowerCase().includes('path') || botData.error.toLowerCase().includes('cli');
    const isGatewayError = errType === 'gateway_error';
    const suggestion = isGatewayError
      ? `<div style="margin-top:8px;font-size:0.78rem;color:var(--text3)">The bot gateway crashed or is restarting — check bot status or restart the LaunchDaemon.</div>`
      : isCliError
        ? `<div style="margin-top:8px;font-size:0.78rem;color:var(--text3)">Suggestions: verify <code>openclaw</code> is installed, check PATH, or run <code>which openclaw</code> in a terminal as the admin user.</div>`
        : `<div style="margin-top:8px;font-size:0.78rem;color:var(--text3)">Check the gateway is running and OC CLI has permission to connect.</div>`;
    panel.innerHTML = `<div style="padding:16px">
      <div style="color:var(--red);font-weight:600;margin-bottom:4px">⚠ Cannot read cron jobs</div>
      <div style="font-size:0.82rem;color:var(--text2)">${escHtml(botData.error)}</div>
      ${suggestion}
    </div>`;
    return;
  }
  panel.innerHTML = _cronBotTable(botData.jobs);
}

async function loadGatewayCron() {
  document.getElementById('cron-table').innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  const cronData = await api('GET', '/api/oc/cron');

  if (!cronData || !Object.keys(cronData).length) {
    document.getElementById('cron-table').innerHTML = `<div class="empty">No cron data available.<br><span style="font-size:0.78rem;color:var(--text3)">OC CLI may not be reachable. Ensure <code>openclaw</code> is installed and in PATH, then refresh.</span></div>`;
    return;
  }

  const botIds = Object.keys(cronData);

  // Choose active tab: keep previous if still valid, else first
  if (!_cronActiveBotTab || !botIds.includes(_cronActiveBotTab)) {
    _cronActiveBotTab = botIds[0];
  }

  // Build per-bot tabs
  const tabsHtml = botIds.map(bid =>
    `<div class="cron-bot-tab subtab${bid === _cronActiveBotTab ? ' active' : ''}"
      data-bot="${escHtml(bid)}"
      onclick="switchCronBotTab('${escHtml(bid)}', _cronLastData)"
      style="cursor:pointer">${escHtml(botLabel(bid))}</div>`
  ).join('');

  document.getElementById('cron-table').innerHTML = `
    <div class="subtabs" style="margin-bottom:12px">${tabsHtml}</div>
    <div class="card" id="cron-bot-panel"></div>`;

  // Store data for tab switching without re-fetch
  window._cronLastData = cronData;

  // Render active tab
  switchCronBotTab(_cronActiveBotTab, cronData);
}

// ══════════════════════════════════════════════════════
// Infra Jobs
// ══════════════════════════════════════════════════════
async function loadInfraJobs() {
  document.getElementById('infrajobs-table').innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  const d = await api('GET', '/api/launchd/jobs');
  const jobs = d?.jobs || [];
  if (!jobs.length) {
    document.getElementById('infrajobs-table').innerHTML = '<div class="empty">No Evolve/OpenClaw launchd jobs found in /Library/LaunchDaemons/.</div>';
    return;
  }
  const rows = jobs.map(j => {
    const loaded = j.loaded
      ? '<span style="color:var(--green)">✓ loaded</span>'
      : '<span style="color:var(--text3)">not loaded</span>';
    const enabled = j.enabled === true
      ? '<span style="color:var(--green)">✓ enabled</span>'
      : j.enabled === false
        ? '<span style="color:var(--text3)">disabled</span>'
        : '<span style="color:var(--text3)">—</span>';
    const pid = j.pid ? `<span style="color:var(--green)">${escHtml(j.pid)}</span>` : '<span style="color:var(--text3)">—</span>';
    const exitCell = j.last_exit === null || j.last_exit === undefined
      ? '<span style="color:var(--text3)">—</span>'
      : j.last_exit === 0
        ? '<span style="color:var(--green)">0</span>'
        : `<span style="color:var(--red)">${escHtml(String(j.last_exit))}</span>`;
    const errNote = j.error ? `<span style="color:var(--red);font-size:0.75rem">${escHtml(j.error)}</span>` : '';
    // Demo of the Phase 0 §4.3.a .resp-table primitive — each <td>
    // carries a data-label that the phone-card view renders. The
    // wrapper + class additions on the table element opt this table
    // into the responsive primitive; nothing else changes at desktop.
    return `<tr>
      <td data-label="Label" style="font-family:monospace;font-size:0.8rem">${escHtml(j.label)}${errNote}</td>
      <td data-label="Schedule" style="font-size:0.75rem;color:var(--text2)">${escHtml(j.schedule)}</td>
      <td data-label="Loaded">${loaded}</td>
      <td data-label="Enabled">${enabled}</td>
      <td data-label="PID" style="font-family:monospace">${pid}</td>
      <td data-label="Last Exit">${exitCell}</td>
    </tr>`;
  });
  document.getElementById('infrajobs-table').innerHTML = `<div class="card"><div class="resp-table-wrap"><table class="resp-table">
    <thead><tr><th>Label</th><th>Schedule</th><th>Loaded</th><th>Enabled</th><th>PID</th><th>Last Exit</th></tr></thead>
    <tbody>${rows.join('')}</tbody>
  </table></div></div>`;
}

// ══════════════════════════════════════════════════════
// OC Version (data now lives in the merged Bot Versions table on the
// Maintenance → System sub-tab; the standalone OC Version tab was retired).
// ══════════════════════════════════════════════════════
let _safetyCheckPolling = null;

// Back-compat shim: legacy callers (overview banner, safety-check polling)
// still call loadOcVersion(). loadSysm() fetches /api/oc/version itself
// and renders both the banner and OC columns, so just delegate.
async function loadOcVersion() {
  return loadSysm();
}

// ── Safe-upgrade preflight (banner button + modal) ─────────────────────
function renderOcUpgradeBanner(d) {
  const sc = d.safety_check;
  const installed = escHtml(d.installed || '?');
  const latest = escHtml(d.latest || '?');
  const running = sc && sc.running;
  const btnLabel = running
    ? '<span class="spinner" style="display:inline-block;width:10px;height:10px;vertical-align:-1px"></span> Checking…'
    : (sc ? 'Re-check' : 'Check OpenClaw update');
  const btnDisabled = running ? 'disabled' : '';

  let pill = '';
  if (running) {
    pill = `<span style="font-size:0.78rem;color:var(--text3)">scan in progress…</span>`;
  } else if (!sc) {
    pill = `<span style="font-size:0.78rem;color:var(--text3)">(no safety check yet)</span>`;
  } else if (sc.ok) {
    const stale = sc.stale ? ' <span style="color:var(--yellow)" title="Installed/latest version changed since this report">· stale</span>' : '';
    pill = `<span style="color:var(--green);font-size:0.82rem;font-weight:600">✅ Safe to upgrade</span>
      · <a href="javascript:openSafetyReport()" style="font-size:0.78rem">view report</a>
      · <span style="font-size:0.74rem;color:var(--text3)">${escHtml(_relativeAge(sc.checked_at))}</span>${stale}`;
  } else {
    const stale = sc.stale ? ' <span style="color:var(--yellow)" title="Installed/latest version changed since this report">· stale</span>' : '';
    pill = `<span style="color:var(--red);font-size:0.82rem;font-weight:600">❌ ${escHtml(sc.summary || 'unsafe')}</span>
      · <a href="javascript:openSafetyReport()" style="font-size:0.78rem">see report</a>
      · <span style="font-size:0.74rem;color:var(--text3)">${escHtml(_relativeAge(sc.checked_at))}</span>${stale}`;
  }

  const upgradeCmd = 'sudo evolve-admin oc upgrade';
  const cmdRow = `<div style="margin-top:8px;font-size:0.78rem;color:var(--text2)">
      Run on the mini:
      <code style="user-select:all;background:rgba(0,0,0,0.25);padding:2px 6px;border-radius:3px;color:var(--text)">${escHtml(upgradeCmd)}</code>
      <button class="btn btn-ghost btn-sm" style="font-size:0.7rem;padding:2px 8px;margin-left:6px"
        onclick="navigator.clipboard.writeText('${escHtml(upgradeCmd)}').then(()=>toast('Copied','ok'))">📋 Copy</button>
    </div>`;

  return `<div style="background:rgba(255,193,7,0.12);border:1px solid var(--yellow);border-radius:6px;padding:10px 14px;font-size:0.85rem;color:var(--yellow)">
    <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
      <div style="flex:1;min-width:240px">⚠️ Update available — installed <strong>${installed}</strong>, latest <strong>${latest}</strong></div>
      <button class="btn btn-primary btn-sm" onclick="runSafetyCheck(false)" ${btnDisabled}>${btnLabel}</button>
    </div>
    <div style="margin-top:8px">${pill}</div>
    ${cmdRow}
  </div>`;
}

function _relativeAge(iso) {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (!t) return '';
  const sec = Math.floor((Date.now() - t) / 1000);
  if (sec < 60) return `checked ${sec}s ago`;
  if (sec < 3600) return `checked ${Math.floor(sec/60)} min ago`;
  if (sec < 86400) return `checked ${Math.floor(sec/3600)}h ago`;
  return `checked ${Math.floor(sec/86400)}d ago`;
}

// Generic relative-time formatter — takes a Date and returns just the
// offset string ("30s ago" / "5m ago" / "2h ago" / "1d ago" / "just now").
// Called from 12+ render functions (errors summary, inbox watcher pill,
// triage rows, intake activity, etc.) that splice the result inline like
// `triaged ${_relTime(new Date(row.triaged_at))}`. Sibling of
// _relativeAge — same arithmetic, no "checked " prefix and accepts a
// Date object rather than an ISO string.
//
// Long-running bug: this was called everywhere but never defined.
// Symptoms only surfaced once the Inbox watcher pill started rendering
// in pods with tracked repos — the throw inside _renderInboxWatcherPill
// was caught by loadInboxRepos's try/catch and reported as a misleading
// "Failed to load tracked repos" error.
function _relTime(date) {
  if (!date) return '';
  const t = date instanceof Date ? date.getTime() : new Date(date).getTime();
  if (!Number.isFinite(t)) return '';
  const sec = Math.floor((Date.now() - t) / 1000);
  if (sec < 0) {
    // Future timestamp — used for things like undo_deadline_at. Flip the
    // sign and prefix with "in " so the caller's "deadline ${_relTime(...)}"
    // reads naturally.
    const future = -sec;
    if (future < 60) return `in ${future}s`;
    if (future < 3600) return `in ${Math.floor(future/60)}m`;
    if (future < 86400) return `in ${Math.floor(future/3600)}h`;
    return `in ${Math.floor(future/86400)}d`;
  }
  if (sec < 5) return 'just now';
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec/60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec/3600)}h ago`;
  return `${Math.floor(sec/86400)}d ago`;
}

async function runSafetyCheck(fromModal) {
  if (_safetyCheckPolling) return;
  const r = await api('POST', '/api/oc/safe-upgrade/check', {});
  if (!r || !r.report_id) {
    const errMsg = '<div class="empty" style="color:var(--red)">Failed to start safety check. Check the admin server logs.</div>';
    if (fromModal) {
      document.getElementById('safety-report-body').innerHTML = errMsg;
    } else {
      const banner = document.getElementById('ocversion-banner');
      if (banner) banner.insertAdjacentHTML('beforeend', errMsg);
    }
    return;
  }
  if (fromModal) {
    document.getElementById('safety-report-body').innerHTML =
      '<div class="loading"><div class="spinner"></div> Re-running check…</div>';
  }
  pollSafetyCheck(r.report_id, fromModal);
}

function pollSafetyCheck(reportId, fromModal) {
  if (_safetyCheckPolling) clearInterval(_safetyCheckPolling);
  loadOcVersion();
  let attempts = 0;
  _safetyCheckPolling = setInterval(async () => {
    attempts++;
    const r = await api('GET', `/api/oc/safe-upgrade/report/${encodeURIComponent(reportId)}`);
    if (r && r.gates) {
      clearInterval(_safetyCheckPolling);
      _safetyCheckPolling = null;
      await loadOcVersion();
      if (fromModal) {
        renderSafetyReportInto(r);
      }
    } else if (attempts > 60) {
      clearInterval(_safetyCheckPolling);
      _safetyCheckPolling = null;
      await loadOcVersion();
    }
  }, 1500);
}

async function openSafetyReport() {
  const modal = document.getElementById('safety-report-modal');
  modal.classList.add('open');
  document.getElementById('safety-report-body').innerHTML =
    '<div class="loading"><div class="spinner"></div> Loading…</div>';
  const r = await api('GET', '/api/oc/safe-upgrade/report/latest');
  if (!r || !r.gates) {
    document.getElementById('safety-report-body').innerHTML =
      '<div class="empty">No safety report available yet. Click "Check OpenClaw update" first.</div>';
    document.getElementById('safety-report-meta').textContent = '';
    return;
  }
  renderSafetyReportInto(r);
}

function closeSafetyReport() {
  document.getElementById('safety-report-modal').classList.remove('open');
}

function renderSafetyReportInto(report) {
  const candidate = report.candidate || {};
  const current = report.current || {};
  const gates = report.gates || {};
  const reqs = report.requirements || [];
  const ok = !!report.ok;

  const inst = current.installed_version || '?';
  const resv = candidate.resolved_version || '?';
  const target = candidate.target_spec || '?';
  document.getElementById('safety-report-meta').textContent =
    `Checked ${report.checked_at} · openclaw ${inst} → ${resv} (target: ${target})`;

  const overall = ok
    ? '<div style="color:var(--green);font-weight:600;margin-bottom:14px">✅ SAFE TO UPGRADE — all gates passed.</div>'
    : `<div style="color:var(--red);font-weight:600;margin-bottom:14px">❌ NOT SAFE — ${escHtml(report.summary || 'blockers must be resolved first')}</div>`;

  const gateOrder = ['node_version', 'stub_install', 'config_references', 'user_launchagents', 'plist_paths', 'port_owners', 'send_surface'];
  const gateLabels = {
    node_version: 'node-version',
    stub_install: 'stub-install',
    config_references: 'config-references',
    user_launchagents: 'user-launchagents',
    plist_paths: 'plist-paths',
    port_owners: 'port-owners',
    send_surface: 'send-surface',
  };
  const gateRows = gateOrder.map(k => {
    const g = gates[k] || {};
    const icon = g.ok ? '<span style="color:var(--green)">✅</span>' : '<span style="color:var(--red)">❌</span>';
    const summary = _gateSummary(k, g);
    return `<div style="display:flex;gap:10px;padding:6px 0;border-bottom:1px solid var(--border);font-size:0.82rem">
      <div style="width:24px;flex-shrink:0">${icon}</div>
      <div style="width:170px;flex-shrink:0;font-family:monospace;color:var(--text2)">${escHtml(gateLabels[k] || k)}</div>
      <div style="flex:1;color:var(--text)">${escHtml(summary)}</div>
    </div>`;
  }).join('');

  let reqsBlock = '';
  if (reqs.length) {
    const items = reqs.map((r, i) => `
      <li style="margin-bottom:10px">
        <div>${escHtml(r.summary || '')}</div>
        ${r.remediation ? `<div style="font-size:0.78rem;color:var(--text2);margin-top:3px;white-space:pre-wrap"><span style="color:var(--yellow)">→</span> ${escHtml(r.remediation)}</div>` : ''}
      </li>`).join('');
    reqsBlock = `
      <div style="margin-top:18px">
        <div style="font-weight:600;margin-bottom:8px;color:var(--text)">Required before upgrade</div>
        <ol style="padding-left:18px;margin:0">${items}</ol>
      </div>`;
  } else if (ok) {
    reqsBlock = '<div style="margin-top:14px;color:var(--text3);font-size:0.82rem">No remediation required.</div>';
  }

  document.getElementById('safety-report-body').innerHTML = `
    ${overall}
    <div style="border-top:1px solid var(--border);padding-top:8px">
      <div style="font-weight:600;margin-bottom:6px;color:var(--text)">Gates</div>
      ${gateRows}
    </div>
    ${reqsBlock}
    <div style="margin-top:14px;font-size:0.7rem;color:var(--text3)">Report id: ${escHtml(report.report_id || '?')}</div>
  `;
}

function _gateSummary(k, g) {
  const d = g.details || {};
  if (k === 'node_version') {
    return `Node ${d.current || '?'} vs engines.node ${JSON.stringify(d.required || null)}`;
  }
  if (k === 'stub_install') {
    return `resolved=${d.resolved_version || '?'}, size=${d.size_kb ?? '?'} KB, bin_present=${d.bin_present}`;
  }
  if (k === 'config_references') {
    if (d.error) return `could not inspect candidate: ${d.error}`;
    const missing = d.missing_by_bot || {};
    const affected = Object.keys(missing);
    const nPlugins = (d.candidate_plugins || []).length;
    if (!affected.length) {
      const nBots = (d.bots || []).length;
      return `candidate ships ${nPlugins} plugins; ${nBots} bot config(s) clean`;
    }
    const all = [...new Set(Object.values(missing).flat())].sort();
    return `missing in candidate: ${all.join(', ')} (affects: ${affected.sort().join(', ')})`;
  }
  if (k === 'user_launchagents') {
    const found = d.found_agents || [];
    const scanned = (d.scanned_users || []).length;
    if (!found.length) return `no orphan agents across ${scanned} bot user(s)`;
    return `orphan agents in: ${[...new Set(found.map(f => f.bot_id))].join(', ')}`;
  }
  if (k === 'plist_paths') {
    const stale = d.stale_plists || [];
    if (!stale.length) return 'all gateway plists resolve';
    return `stale plists: ${stale.join(', ')}`;
  }
  if (k === 'port_owners') {
    const ports = d.ports || [];
    const bad = ports.filter(p => !p.ok);
    if (!bad.length) return `all ${ports.length} gateway port(s) owned correctly`;
    return `${bad.length} of ${ports.length} port(s) misowned`;
  }
  if (k === 'send_surface') {
    if (d.status === 'ok') return 'message-send contract verified on the installed CLI';
    if (d.status === 'failed') {
      const missing = (d.missing_flags || []).join(', ');
      return `broken: ${d.reason || '?'}${missing ? ` (missing: ${missing})` : ''}`;
    }
    return `couldn't verify: ${d.reason || '?'}`;
  }
  return '';
}

// ══════════════════════════════════════════════════════
// Gateway Log Viewer
// ══════════════════════════════════════════════════════
let _logsActiveBotId = null;
let _logsAutoRefreshTimer = null;
let _logsBotList = [];

function toggleLogsAutoRefresh() {
  if (_logsAutoRefreshTimer) { clearInterval(_logsAutoRefreshTimer); _logsAutoRefreshTimer = null; }
  if (document.getElementById('logs-autorefresh').checked) {
    _logsAutoRefreshTimer = setInterval(() => loadLogsForBot(_logsActiveBotId), 10000);
  }
}

async function loadLogsForBot(botId) {
  if (!botId) return;
  _logsActiveBotId = botId;
  document.querySelectorAll('.logs-bot-tab').forEach(el => {
    el.classList.toggle('active', el.dataset.bot === botId);
  });
  const panel = document.getElementById('logs-panel');
  panel.textContent = 'Loading…';
  const d = await api('GET', `/api/gateway/logs/${encodeURIComponent(botId)}`);
  document.getElementById('logs-path').textContent = d?.log_path ? `Path: ${d.log_path}` : '';
  if (d?.error && !d.lines?.length) {
    panel.innerHTML = `<span style="color:var(--red)">${escHtml(d.error)}</span>`;
    return;
  }
  const lines = d?.lines || [];
  if (!lines.length) { panel.textContent = '(no log output)'; return; }
  panel.textContent = lines.join('\n');
  panel.scrollTop = panel.scrollHeight;
}

async function loadGatewayLogs() {
  const d = await api('GET', '/api/network');
  const bots = orderedBotIds(d?.bots);
  _logsBotList = bots;
  if (!bots.length) {
    document.getElementById('logs-bot-tabs').innerHTML = '';
    document.getElementById('logs-panel').textContent = 'No bots configured.';
    return;
  }
  if (!_logsActiveBotId || !bots.includes(_logsActiveBotId)) _logsActiveBotId = bots[0];
  document.getElementById('logs-bot-tabs').innerHTML = bots.map(bid =>
    `<div class="logs-bot-tab subtab${bid === _logsActiveBotId ? ' active' : ''}" data-bot="${escHtml(bid)}"
      onclick="loadLogsForBot('${escHtml(bid)}')" style="cursor:pointer">${escHtml(botLabel(bid))}</div>`
  ).join('');
  await loadLogsForBot(_logsActiveBotId);
}

// ══════════════════════════════════════════════════════
// Analytics — Turns
// ══════════════════════════════════════════════════════
let _turnsRawOffset = 0;
const _turnsRawLimit = 50;

async function loadAnalyticsTurns() {
  const bot = document.getElementById('turns-bot')?.value || '';
  const days = document.getElementById('turns-days')?.value || 30;
  const d = await api('GET', `/api/analytics/turns?bot=${bot}&days=${days}`);

  const summary = { turns: 0, cost: 0, token_turns: 0, api_key_turns: 0 };
  const daily = {};
  const by_source = {};
  const by_model = {};
  const by_channel = {};

  Object.values(d || {}).forEach(bd => {
    if (!bd || !bd.summary) return;
    const s = bd.summary;
    summary.turns += s.turns || 0;
    summary.cost += s.cost || 0;
    summary.token_turns += s.token_turns || 0;
    summary.api_key_turns += s.api_key_turns || 0;
    (bd.daily || []).forEach(row => {
      if (!daily[row.date]) daily[row.date] = { date: row.date, turns: 0, cost: 0, token_turns: 0, api_key_turns: 0 };
      daily[row.date].turns += row.turns || 0;
      daily[row.date].cost += row.cost || 0;
      daily[row.date].token_turns += row.token_turns || 0;
      daily[row.date].api_key_turns += row.api_key_turns || 0;
    });
    Object.entries(bd.by_source || {}).forEach(([k,v]) => { by_source[k] = (by_source[k]||0)+v; });
    Object.entries(bd.by_model || {}).forEach(([k,v]) => { by_model[k] = (by_model[k]||0)+v; });
    Object.entries(bd.by_channel || {}).forEach(([k,v]) => { by_channel[k] = (by_channel[k]||0)+v; });
  });

  const tokenPct = summary.turns ? Math.round((summary.token_turns / summary.turns) * 100) : 0;
  const apiPct = summary.turns ? Math.round((summary.api_key_turns / summary.turns) * 100) : 0;
  document.getElementById('turns-stats').innerHTML = `
    <div class="stat-block"><div class="stat-value">${summary.turns.toLocaleString()}</div><div class="stat-label">Total Turns (${days}d)</div></div>
    <div class="stat-block"><div class="stat-value" style="color:var(--green)">${tokenPct}%</div><div class="stat-label">MAX Turns (free)</div></div>
    <div class="stat-block"><div class="stat-value" style="color:${apiPct>5?'var(--red)':'var(--text3)'}">${apiPct}%</div><div class="stat-label">API Key Turns</div></div>
    <div class="stat-block"><div class="stat-value" style="color:var(--yellow)">${_fmtUsd(summary.cost)}</div><div class="stat-label">Est. Total Cost</div></div>`;

  const sorted = Object.values(daily).sort((a,b) => a.date.localeCompare(b.date));
  const labels = sorted.map(r => r.date.slice(5));
  mkChart('chart-turns-daily', 'bar', {
    labels,
    datasets: [
      { label: 'MAX (token)', data: sorted.map(r => r.token_turns), backgroundColor: 'rgba(74,222,128,0.5)', borderColor: 'var(--green)', borderWidth: 1, stack: 's' },
      { label: 'API Key', data: sorted.map(r => r.api_key_turns), backgroundColor: 'rgba(248,113,113,0.5)', borderColor: 'var(--red)', borderWidth: 1, stack: 's' },
    ]
  }, { scales: { x: { stacked: true, ticks: { color: '#666', font: { size: 10 } }, grid: { color: '#222' } }, y: { stacked: true, ticks: { color: '#666', font: { size: 10 } }, grid: { color: '#222' } } } });

  const srcLabels = Object.keys(by_source);
  mkChart('chart-turns-source', 'doughnut', {
    labels: srcLabels,
    datasets: [{ data: srcLabels.map(k => by_source[k]), backgroundColor: ['#4ade80','#fb923c','#7eb8f7','#c084fc','#fbbf24'], borderWidth: 0 }]
  });

  const mdlLabels = Object.keys(by_model).sort((a,b) => by_model[b]-by_model[a]).slice(0,8);
  mkChart('chart-turns-model', 'bar', {
    labels: mdlLabels.map(m => m.split('/').pop()),
    datasets: [{ label: 'Turns', data: mdlLabels.map(k => by_model[k]), backgroundColor: 'rgba(126,184,247,0.4)', borderColor: 'var(--blue)', borderWidth: 1 }]
  }, { indexAxis: 'y' });

  const chLabels = Object.keys(by_channel);
  mkChart('chart-turns-channel', 'doughnut', {
    labels: chLabels,
    datasets: [{ data: chLabels.map(k => by_channel[k]), backgroundColor: ['#2dd4bf','#fb923c','#c084fc','#fbbf24','#f87171'], borderWidth: 0 }]
  });

  // Populate raw turns bot selector
  const rawBotSel = document.getElementById('turns-raw-bot');
  if (rawBotSel) {
    const botKeys = Object.keys(d || {});
    while (rawBotSel.options.length > 1) rawBotSel.remove(1);
    botKeys.forEach(b => rawBotSel.add(new Option(b, b)));
  }
}

async function loadTurnsRaw() {
  _turnsRawOffset = 0;
  await _fetchTurnsRaw();
}

async function _fetchTurnsRaw() {
  const bot = document.getElementById('turns-raw-bot')?.value || '';
  const dateVal = document.getElementById('turns-raw-date')?.value || '';
  if (!bot) { document.getElementById('turns-raw-table').innerHTML = '<div class="empty">Select a bot above.</div>'; return; }
  const params = `bot=${bot}&date=${dateVal}&limit=${_turnsRawLimit}&offset=${_turnsRawOffset}`;
  const d = await api('GET', `/api/analytics/turns/raw?${params}`);
  const turns = d.turns || [];
  const total = d.total || 0;
  const el = document.getElementById('turns-raw-table');
  if (!turns.length) {
    el.innerHTML = '<div class="empty">No turns found for that date.</div>';
    document.getElementById('turns-raw-pagination').innerHTML = '';
    return;
  }
  el.innerHTML = '<div class="resp-table-wrap"><table class="resp-table"><thead><tr><th>Time</th><th>Source</th><th>Channel</th><th>Model</th><th>Auth</th><th>Cost</th><th>Tokens</th></tr></thead><tbody>' +
    turns.map(t => `<tr>
      <td style="font-size:0.73rem;color:var(--text2)">${(t.ts||'').replace('T',' ').slice(0,19)}</td>
      <td>${badge(t.source||'?','member')}</td>
      <td style="font-size:0.78rem;color:var(--text2)">${t.channel||'—'}</td>
      <td style="font-family:monospace;font-size:0.73rem;color:var(--teal)">${(t.model||'—').split('/').pop()}</td>
      <td>${badge(t.auth_mode||'?', t.auth_mode==='token'?'ok':t.auth_mode==='api_key'?'warn':'member')}</td>
      <td style="font-size:0.78rem">${(t.cost||0)>0?_fmtUsd(t.cost):'—'}</td>
      <td style="font-size:0.73rem;color:var(--text2)">${((t.input_tokens||0)+(t.output_tokens||0)).toLocaleString()}</td>
    </tr>`).join('') + '</tbody></table></div>';
  _respTableLabelize(el);
  const pages = Math.ceil(total / _turnsRawLimit);
  const cur = Math.floor(_turnsRawOffset / _turnsRawLimit) + 1;
  document.getElementById('turns-raw-pagination').innerHTML = `
    <span>${total} total · page ${cur} of ${Math.max(pages,1)}</span>
    ${_turnsRawOffset > 0 ? `<button class="btn btn-ghost btn-sm" onclick="_turnsRawOffset=Math.max(0,_turnsRawOffset-${_turnsRawLimit});_fetchTurnsRaw()">← Prev</button>` : ''}
    ${_turnsRawOffset + _turnsRawLimit < total ? `<button class="btn btn-ghost btn-sm" onclick="_turnsRawOffset+=${_turnsRawLimit};_fetchTurnsRaw()">Next →</button>` : ''}`;
}

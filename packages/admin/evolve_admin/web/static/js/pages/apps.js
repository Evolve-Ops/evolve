// ════════════════════════════════════════════════════════════════════════
// Page: Apps (Capabilities subtab + App-lifecycle actions)
//
// The Installed/Capabilities subtab of the Apps page. Renders the
// per-bot grid of installed applications, with per-app recommendations,
// scan progress strip, test rollup card, coherence + reconciliation
// chips, manifest viewer modal, lessons viewer, and the share-lessons
// action.
//
// State (top of the file):
//   _capBot, _capRecs, _capData — per-bot view state
//   plus the lifecycle-side adopt / delete / pause / archive state
//
// Lookup tables: status colors, health buckets, severity rank.
//
// Loaders dispatched via onPageActivate('apps') + subtab activators:
//   loadCapabilities()                   — bot grid (re-fetches every
//                                          time onSubTabActivate fires
//                                          'apps' / 'installed')
//   loadCapabilityRecommendations(bot)   — banner above the grid
//
// App-lifecycle actions (the second cluster, "APP LIFECYCLE —
// PAUSE / ARCHIVE / RESTORE + UNINSTALL WIZARD"):
//   - Quick single-action helpers (_capName etc.)
//   - Pause / Archive / Restore flows
//   - Uninstall wizard (multi-step modal)
//   - Adopt (orphan-app reconcile)
//   - Delete (with breakdown preview)
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - viewManifest is called by inline onclick attributes elsewhere
//     in the SPA (Forge Jobs row, Backup detail) — those resolve at
//     click time and continue to find the function as a global.
//   - The action flows call api(), toast(), botLabel(), escHtml() —
//     all in core/.
//
// Out of scope (separate clusters, future phases):
//   loadGallery / _renderGallery* / Gallery details modal
//     (~lines 44219+) — Gallery subtab; thousands of lines, separate
//     cluster, future phase.
//   loadForgeJobs / renderForgeJobs / forgeApprove ...
//     — already extracted in Phase 3c (pages/forge.js).
//
// Tasks (REMOVED — Continuity Engine v1) section header sits right
// after this cluster; the orphaned section header stays in index.html
// since the comment block documents the removal.
// ════════════════════════════════════════════════════════════════════════


// ══════════════════════════════════════════════════════
// Capabilities
// ══════════════════════════════════════════════════════
let _capBot = null;
let _capRecs = [];   // active recommendations for the current bot

function _capRenderTabs() {
  const bots = orderedBotIds(_statusData?.bots);
  const tabsEl = document.getElementById('cap-bot-tabs');
  if (!tabsEl) return;
  if (!_capBot || !bots.includes(_capBot)) _capBot = bots[0] || null;
  tabsEl.innerHTML = bots.map(b =>
    `<div class="subtab ${b === _capBot ? 'active' : ''}" data-bot="${escHtml(b)}" onclick="capSwitchBot('${escHtml(b)}')">${escHtml(botLabel(b))}</div>`
  ).join('');
}

function capSwitchBot(b) {
  _capBot = b;
  _capRenderTabs();
  loadCapabilities();
}

async function loadCapabilities() {
  _capRenderTabs();
  const bot = _capBot;
  if (!bot) { document.getElementById('cap-grid').innerHTML = '<div class="empty">Select a bot to view applications.</div>'; _renderCapabilitiesAffordance([], ''); return; }
  const d = await api('GET', `/api/analytics/applications?bot=${bot}`);
  _capData = d[bot] || [];
  renderCapabilities();
  // Snapshot for evo's apps pack (spec §3.4 reliability lever #3).
  // Captures app list + counts for the bot the operator is currently
  // viewing so evo can answer "what apps does <bot> have?" without a
  // separate fetch.
  try {
    window._evoContextSnapshots = window._evoContextSnapshots || {};
    window._evoContextSnapshots.apps = {
      bot,
      total: _capData.length,
      top: _capData.slice(0, 8).map(c => ({
        name: c.name || c.id || '?',
        kind: c.kind || c.type || null,
        last_used: c.last_used || null,
        schema_version: c.schema_version || null,
      })),
    };
    if (typeof _evoDrawerUpdateContextChip === 'function') _evoDrawerUpdateContextChip();
  } catch (_) {}
  // v13 — surface a re-scan reminder for apps whose schema predates
  // scheduled-action extraction. Spec: docs/spec-audit-extensions-2026-05-17.md §3.5.
  _renderCapRescanStrip(bot);
  // Load recommendations in parallel — failure is non-fatal
  loadCapabilityRecommendations(bot);
}

// v13 — show the "needs re-scan" strip when any manifest on this bot is
// on a schema older than the current scanner. One-line headline + a
// toggleable list with per-bot "Run scan now" buttons.
function _renderCapRescanStrip(bot) {
  const strip = document.getElementById('cap-rescan-strip');
  if (!strip) return;
  // Schema version below which the new scheduled-action fields are absent.
  // Anything < 13 is missing the v13 surface; the scanner re-run populates
  // it (or confirms emptiness when the bot truly has no scheduled actions).
  const RESCAN_THRESHOLD = 13;
  const stale = (_capData || []).filter(c => {
    const v = c.schema_version || 0;
    return v > 0 && v < RESCAN_THRESHOLD;
  });
  if (!stale.length) { strip.style.display = 'none'; return; }
  strip.style.display = '';
  const head = document.getElementById('cap-rescan-headline');
  if (head) {
    head.textContent = `${stale.length} app${stale.length === 1 ? '' : 's'} on this bot need${stale.length === 1 ? 's' : ''} re-scan to pick up new audit fields (scheduled-action contracts).`;
  }
  const list = document.getElementById('cap-rescan-list');
  if (list) {
    list.innerHTML = stale.map(c => `
      <div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--border);font-size:0.78rem">
        <span style="flex:1;color:var(--text)">${escHtml(c.name || c.id || '?')}</span>
        <span style="color:var(--text3);font-size:0.72rem">schema v${c.schema_version || '?'}</span>
      </div>
    `).join('') + `
      <div style="margin-top:10px;display:flex;gap:8px;align-items:center;font-size:0.78rem;color:var(--text2)">
        Re-scanning this bot updates every manifest at once.
        <button class="btn btn-ghost btn-sm" onclick="runCapabilityScan()">↻ Run scan now</button>
      </div>`;
    list.style.display = 'none';
  }
}

// Toggle the per-app list under the "needs re-scan" strip.
function capRescanToggleList() {
  const list = document.getElementById('cap-rescan-list');
  const btn = document.getElementById('cap-rescan-toggle');
  if (!list || !btn) return;
  if (list.style.display === 'none') {
    list.style.display = '';
    btn.textContent = 'Hide list';
  } else {
    list.style.display = 'none';
    btn.textContent = 'Show list';
  }
}

async function loadCapabilityRecommendations(bot) {
  try {
    const d = await api('GET', `/api/bots/${bot}/recommendations?status=new,surfaced`);
    _capRecs = d.recommendations || [];
    renderCapabilityRecommendations(bot);
  } catch(e) {
    // Recommendations not critical — hide section on error
    const sec = document.getElementById('cap-recs-section');
    if (sec) sec.style.display = 'none';
  }
}

function renderCapabilityRecommendations(bot) {
  const sec = document.getElementById('cap-recs-section');
  const grid = document.getElementById('cap-recs-grid');
  const botName = document.getElementById('cap-recs-bot-name');
  const countEl = document.getElementById('cap-recs-count');
  if (!sec || !grid) return;

  if (!_capRecs.length) { sec.style.display = 'none'; return; }

  sec.style.display = '';
  if (botName) botName.textContent = bot;
  if (countEl) countEl.textContent = _capRecs.length;

  grid.innerHTML = _capRecs.map(r => _renderRecCard(r, bot)).join('');
}

function _renderRecCard(r, bot) {
  const esc = escHtml;
  const icons = {
    install_existing: '🏪',
    new_capability:   '✨',
    novel_concept:    '💡',
    fix_effectiveness:'⚠️',
    reactivate:       '💤',
  };
  const icon = icons[r.category] || '📋';

  const confidencePct = r.confidence != null ? Math.round(r.confidence * 100) + '%' : null;
  const matchStrength = r.match_strength
    ? `<span style="font-size:0.7rem;color:var(--text2);font-weight:500">${esc(r.match_strength.charAt(0).toUpperCase() + r.match_strength.slice(1))} match</span>`
    : '';
  const sourceBadge = `<span style="font-size:0.68rem;color:var(--text3);background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:1px 6px">${esc(r.source)}</span>`;

  // Evidence line
  let evidenceLine = '';
  if (r.category === 'new_capability' && r.evidence?.session_count) {
    evidenceLine = `<div style="font-size:0.75rem;color:var(--text2);margin-bottom:6px">Seen in ${r.evidence.session_count} sessions</div>`;
  } else if (r.category === 'install_existing' && r.evidence?.matching_tags?.length) {
    evidenceLine = `<div style="font-size:0.75rem;color:var(--text2);margin-bottom:6px">Matches: ${r.evidence.matching_tags.map(t => esc(t)).join(', ')}</div>`;
  }

  // Action buttons
  let actions = '';
  if (r.category === 'install_existing') {
    const pkgId = r.payload?.gallery_pkg_id;
    if (pkgId) {
      actions = `<button class="btn btn-primary btn-sm" style="margin-right:6px" onclick="openGalleryInstall('${esc(pkgId)}')">Install</button>`;
    }
  }
  actions += `<button class="btn btn-ghost btn-sm" onclick="dismissRecommendation('${esc(bot)}','${esc(r.rec_id)}',this)">Dismiss</button>`;

  return `<div class="cap-card" data-rec-id="${esc(r.rec_id)}" style="border-left:3px solid var(--accent)">
    <div class="cap-card-head" style="margin-bottom:4px">
      <span style="font-size:1.1rem;margin-right:6px">${icon}</span>
      <div class="cap-card-name" title="${esc(r.title)}">${esc(r.title)}</div>
    </div>
    <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px">${sourceBadge}${matchStrength ? ' ' + matchStrength : ''}</div>
    ${r.rationale ? `<div style="font-size:0.78rem;color:var(--text2);margin-bottom:6px">${esc(r.rationale.slice(0, 120))}${r.rationale.length > 120 ? '…' : ''}</div>` : ''}
    ${evidenceLine}
    <div style="margin-top:10px;display:flex;align-items:center;gap:6px">${actions}</div>
  </div>`;
}

async function dismissRecommendation(botId, recId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Dismissing…'; }
  const res = await api('POST', `/api/bots/${botId}/recommendations/${recId}/status`, { status: 'dismissed' });
  if (res.error) { toast('✗ ' + res.error, 'err'); if (btn) { btn.disabled = false; btn.textContent = 'Dismiss'; } return; }
  // Remove card from DOM
  const card = document.querySelector(`[data-rec-id="${recId}"]`);
  if (card) card.remove();
  _capRecs = _capRecs.filter(r => r.rec_id !== recId);
  const countEl = document.getElementById('cap-recs-count');
  if (countEl) countEl.textContent = _capRecs.length;
  if (!_capRecs.length) {
    const sec = document.getElementById('cap-recs-section');
    if (sec) sec.style.display = 'none';
  }
  toast('Recommendation dismissed');
}

let _scanPollTimer = null;
let _scanStartTime = null;

function _renderScanProgress(s) {
  const phase = s.phase || 1;
  // Default to 8 — matches PHASE_TOTAL in scanner.py since 2026-06-08.
  // Was 4; bumped when post-manifest passes (file_stamp / layer_classify /
  // reconcile / coherence_pass_a) were added to SCAN_PHASES. If you bump
  // PHASE_TOTAL in scanner.py, update this default too.
  const phaseTotal = s.phase_total || 8;
  const desc = s.phase_desc || 'Scanning workspace…';
  const eta = s.eta_seconds != null ? Math.max(0, Math.round(s.eta_seconds)) : null;
  const found = s.found || 0;
  const created = s.manifests_created || 0;
  const total = s.manifests_total || 0;

  const elapsed = _scanStartTime ? Math.round((Date.now() - _scanStartTime) / 1000) : 0;
  const elapsedStr = elapsed >= 60
    ? `${Math.floor(elapsed/60)}m ${elapsed%60}s`
    : `${elapsed}s`;
  const etaStr = eta != null
    ? (eta < 60 ? `~${eta}s remaining` : `~${Math.ceil(eta/60)}m remaining`)
    : 'estimating…';

  // Fine-grained progress:
  //   Phases 1–(phaseTotal-2): fill 0→75% linearly
  //   Phase (phaseTotal-1)   : fill 75→97% by manifest count (manifest generation)
  //   Phase phaseTotal       : fill 97→99% (file stamping — brief, show nearly-done)
  const basePhases = Math.max(phaseTotal - 2, 1); // phases before manifest generation
  let pct;
  if (phase < phaseTotal - 1) {
    pct = Math.round(((phase - 1) / basePhases) * 75);
  } else if (phase === phaseTotal - 1) {
    const manifestPct = total > 0 ? (created / total) : 0;
    pct = Math.round(75 + manifestPct * 22);
  } else {
    // Final phase (file stamping): pulse between 97–99% so bar doesn't appear stalled at 100%
    pct = 97;
  }
  // Phase names match SCAN_PHASES in scanner.py (PHASE_TOTAL = 8 since
  // 2026-06-08). If you renumber phases there, mirror it here.
  const phaseNames = [
    'Inventory', 'AI Discovery', 'Merge', 'Manifests',
    'File Stamp', 'Layer Classify', 'Reconcile', 'Coherence',
  ];
  const phaseName = phaseNames[phase - 1] || `Phase ${phase}`;

  // Phase step dots
  const dots = Array.from({length: phaseTotal}, (_, i) => {
    const n = i + 1;
    const cls = n < phase ? 'done' : n === phase ? 'active' : 'pending';
    return `<div class="scan-phase-dot ${cls}" title="${phaseNames[i] || 'Phase '+(i+1)}"></div>`;
  }).join('');

  const currentApp = s.current_app_name || '';
  const currentAppNum = s.current_app_num || 0;

  // "found" = total apps discovered this scan (new + existing)
  // "total" = new manifests to generate this scan
  // Show them with clear labels so "2 / 2 new" doesn't look like "only 2 apps exist"
  let manifestLine = '';
  if (total > 0) {
    const appLabel = found > total
      ? `${found} apps found, ${total} new`
      : `${total} new app${total !== 1 ? 's' : ''}`;
    const inProgress = currentApp && created < total
      ? ` — <em>${currentApp.replace(/[<>&"]/g,'')}</em> (${currentAppNum}/${total})`
      : '';
    manifestLine = `<div style="font-size:0.78rem;color:var(--text3);margin-top:4px">${appLabel}: ${created}/${total} manifest${total !== 1 ? 's' : ''} generated${inProgress}</div>`;
  } else if (found > 0) {
    manifestLine = `<div style="font-size:0.78rem;color:var(--text3);margin-top:4px">${found} existing app${found !== 1 ? 's' : ''} found — no new manifests needed</div>`;
  }

  const aiNote = phase === 2
    ? `<div style="font-size:0.73rem;color:var(--accent);margin-top:8px;font-style:italic">AI is reading workspace files — this takes 20-40 seconds</div>`
    : phase === 4
    ? `<div style="font-size:0.73rem;color:var(--accent);margin-top:8px;font-style:italic">Generating detailed manifests — ~15s per application</div>`
    : phase === 5
    ? `<div style="font-size:0.73rem;color:var(--accent);margin-top:8px;font-style:italic">Registering component files in manifests — almost done</div>`
    : '';

  return `<div style="text-align:center;padding:32px 24px;grid-column:1/-1;max-width:520px;margin:0 auto">
    <div style="font-weight:600;font-size:1rem;margin-bottom:6px">Scanning workspace for applications</div>
    <div style="font-size:0.82rem;color:var(--text2);margin-bottom:16px">${desc}</div>

    <div style="background:rgba(255,255,255,0.08);border-radius:8px;height:8px;margin-bottom:12px;overflow:hidden">
      <div style="background:#22c55e;height:100%;width:${pct}%;border-radius:8px"></div>
    </div>

    <div class="scan-phase-dots" style="display:flex;justify-content:center;gap:8px;margin-bottom:12px">
      ${dots}
    </div>

    <div style="font-size:0.8rem;font-weight:600;color:var(--text1);margin-bottom:2px">${phaseName} — Phase ${phase} of ${phaseTotal}</div>
    ${manifestLine}
    ${aiNote}

    <div style="display:flex;justify-content:center;gap:20px;margin-top:12px;font-size:0.72rem;color:var(--text3)">
      <span>Elapsed: ${elapsedStr}</span>
      <span>${etaStr}</span>
    </div>

    <div style="margin-top:16px;font-size:0.7rem;color:var(--text3)">
      The scan is running in the background. Results appear automatically when complete.
    </div>
  </div>`;
}

async function runCapabilityScan(quick = false) {
  const bot = _capBot || '';
  if (!bot) { toast('Select a bot first', 'err'); return; }
  const url = `/api/applications/scan?bot=${bot}` + (quick ? '&quick=1' : '');
  const r = await api('POST', url);
  if (r.error) { toast('✗ ' + r.error, 'err'); return; }
  if (r.status === 'already_running') { toast('Scan already in progress', 'warn'); return; }

  const scanBtn = document.getElementById('cap-scan-btn');
  if (scanBtn) { scanBtn.disabled = true; scanBtn.textContent = 'Scanning…'; }

  _scanStartTime = Date.now();
  const el = document.getElementById('cap-grid');
  // phase_total stays at 8 throughout the scan now (was the source of
  // the "1/5" → "1/8" flash before 2026-06-08; the post-manifest phases
  // are part of SCAN_PHASES, so the total doesn't change mid-scan).
  el.innerHTML = _renderScanProgress({phase: 1, phase_total: 8, phase_desc: 'Starting scan…', eta_seconds: quick ? 5 : 70});

  let _pollCount = 0;
  if (_scanPollTimer) clearInterval(_scanPollTimer);
  _scanPollTimer = setInterval(async () => {
    const s = await api('GET', `/api/applications/scan/status?bot=${bot}`);
    _pollCount++;
    if (s.status === 'done' || s.status === 'error') {
      clearInterval(_scanPollTimer); _scanPollTimer = null;
      _scanStartTime = null;
      if (scanBtn) { scanBtn.disabled = false; scanBtn.textContent = '↻ Re-scan'; }
      if (s.status === 'done') {
        const msg = `✓ Scan complete — ${s.count} application${s.count !== 1 ? 's' : ''} found`;
        toast(msg, s.count === 0 ? 'warn' : 'ok');
      } else {
        toast('✗ Scan failed — see log below', 'err');
      }
      // Show log button whenever we have a log
      const logBtn = document.getElementById('cap-scan-log-btn');
      if (logBtn && s.log) logBtn.style.display = 'inline-block';
      await loadCapabilities();
    } else if (s.status === 'running') {
      // Detect stale bot code: after several polls, if phase_total is still 4 and no
      // scanner log has appeared, the bot process is running old code and needs a restart.
      const staleCode = _pollCount >= 6 && (s.phase_total || 4) <= 4 && !s.log;
      el.innerHTML = _renderScanProgress(s) +
        (staleCode ? `<div style="margin-top:8px;padding:8px 12px;background:rgba(255,180,0,0.12);border:1px solid rgba(255,180,0,0.3);border-radius:6px;font-size:0.72rem;color:var(--yellow,#f59e0b);text-align:center;grid-column:1/-1">
          ⚠ Bot process may be running old code — restart the bot to pick up scanner updates.<br>
          <code style="font-size:0.7rem">sudo /bin/launchctl kickstart -k system/ai.evolve.evolve.${bot}</code>
        </div>` : '');
      // Show log button as soon as the scanner starts emitting a log (Phase 5+)
      const logBtn = document.getElementById('cap-scan-log-btn');
      if (logBtn && s.log) logBtn.style.display = 'inline-block';
    }
  }, 2500);
}

async function showScanLog() {
  const bot = _capBot || '';
  if (!bot) return;
  const log = await fetch(`/api/applications/scan/log?bot=${bot}`).then(r => r.text());
  const pre = document.createElement('pre');
  pre.style.cssText = 'max-height:60vh;overflow:auto;background:var(--bg1);padding:14px;border-radius:8px;font-size:0.75rem;white-space:pre-wrap;word-break:break-all';
  pre.textContent = log || '(no log)';
  const modal = document.createElement('div');
  modal.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center';
  modal.innerHTML = `<div style="background:var(--bg2);border-radius:12px;padding:20px;max-width:800px;width:90%;max-height:80vh;overflow:auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <strong>Scan log — ${escHtml(botLabel(bot))}</strong>
      <button class="btn btn-ghost btn-sm" onclick="this.closest('[style*=fixed]').remove()">✕ Close</button>
    </div></div>`;
  modal.querySelector('div').appendChild(pre);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  document.body.appendChild(modal);
}

function showNewCapabilityForm() { document.getElementById('new-cap-form').style.display = 'block'; }
function hideNewCapabilityForm() { document.getElementById('new-cap-form').style.display = 'none'; document.getElementById('ncap-result').innerHTML = ''; }
function updateCapId() {
  const n = document.getElementById('ncap-name').value;
  document.getElementById('ncap-id').value = n.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
}
async function saveCapability() {
  const bot = _capBot || '';
  if (!bot) { toast('Select a bot first', 'err'); return; }
  const name = document.getElementById('ncap-name').value.trim();
  const id = document.getElementById('ncap-id').value.trim();
  if (!name || !id) { toast('Name and ID required', 'err'); return; }
  const r = await api('POST', '/api/applications', {
    bot_id: bot, id, name,
    description: document.getElementById('ncap-desc').value.trim(),
    priority: 'feature',
    test_trigger: document.getElementById('ncap-trigger').value.trim(),
    expected_keywords: document.getElementById('ncap-expected').value.trim() ? [document.getElementById('ncap-expected').value.trim()] : [],
  });
  if (r.ok) { toast('✓ Application saved', 'ok'); hideNewCapabilityForm(); await loadCapabilities(); }
  else { document.getElementById('ncap-result').innerHTML = `<span style="color:var(--red)">✗ ${r.error||'Failed'}</span>`; }
}

// App-test rollup removed 2026-06-08 — surface killed per
// docs/decision-app-tests-2026-06-08.md. Audit + coherence already cover
// what tests were meant to catch; production state confirmed the surface
// was never live (2% of manifests carried a test_command, scheduler off).
function _renderCapTestRollup() {
  const el = document.getElementById('cap-test-rollup');
  if (el) el.style.display = 'none';
}

// ── Coherence + Drift chip helpers (spec §10) ─────────────────────────────
//
// Read summary fields populated by /api/analytics/applications.
// Click → opens the manifest modal, scrolled to the Coherence + Drift
// section. The chip on the list view is "see-status-at-a-glance"; the
// drilldown lives in the modal.
function _coherenceChip(c) {
  const status = c.coherence_status || 'ok';
  if (status === 'ok') return '';
  const n = c.coherence_findings_count || 0;
  const crit = c.coherence_critical_count || 0;
  const bot = _capBot || '';
  const onclick = `onclick="_openCoherenceModal('${bot}','${escHtml(c.id)}')"`;
  if (status === 'incoherent') {
    const tip = `${crit} critical / major coherence finding${crit === 1 ? '' : 's'}. Click to view + repair.`;
    return `<span class="badge badge-inline" style="font-size:0.65rem;background:rgba(220,38,38,0.15);color:var(--red);border:1px solid rgba(220,38,38,0.35);cursor:pointer" title="${escHtml(tip)}" ${onclick}>✗ incoherent</span>`;
  }
  // warnings (minor / info)
  const tip = `${n} coherence warning${n === 1 ? '' : 's'}. Click to view.`;
  return `<span class="badge badge-inline" style="font-size:0.65rem;background:rgba(245,158,11,0.12);color:var(--yellow);border:1px solid rgba(245,158,11,0.3);cursor:pointer" title="${escHtml(tip)}" ${onclick}>⚠ ${n} warning${n === 1 ? '' : 's'}</span>`;
}

function _reconciliationChip(c) {
  const status = c.reconciliation_status || 'ok';
  if (status === 'ok' || status === 'pending' || !status) return '';
  const bot = _capBot || '';
  const onclick = `onclick="_openCoherenceModal('${bot}','${escHtml(c.id)}')"`;
  if (status === 'orphan') {
    const tip = 'Reconciliation marked this app as orphan — files / crons / actions are missing. Click to choose Reinstall / Archive / Convert-to-template.';
    return `<span class="badge badge-inline" style="font-size:0.65rem;background:rgba(220,38,38,0.12);color:var(--red);border:1px solid rgba(220,38,38,0.3);cursor:pointer" title="${escHtml(tip)}" ${onclick}>▼ orphan</span>`;
  }
  if (status === 'drift') {
    const drifted = c.reconciliation_drifted_count || 0;
    const added = c.reconciliation_added_count || 0;
    const removed = c.reconciliation_removed_count || 0;
    const parts = [];
    if (drifted) parts.push(`${drifted} drifted`);
    if (added)   parts.push(`+${added}`);
    if (removed) parts.push(`-${removed}`);
    const tip = `Manifest disagrees with disk (${parts.join(', ')}). Click to approve / promote / flag.`;
    return `<span class="badge badge-inline" style="font-size:0.65rem;background:rgba(59,130,246,0.12);color:var(--blue);border:1px solid rgba(59,130,246,0.3);cursor:pointer" title="${escHtml(tip)}" ${onclick}>↻ drift${drifted ? ' (' + drifted + ')' : ''}</span>`;
  }
  return '';
}

// ── Reliability chip + "Make reliable" flow ─────────────────────────────────
// spec-agent-freelance-bypass-phase2-2026-06-06, re-homed by Bite 2a of the
// just-works integrity arc (spec-app-invocation-just-works-2026-06-29 §2.2/§3).
// The server's bot_guidance_freelance_validator flags an at-risk-shaped
// manifest still running in invocation_mode "agent_invokes" as severity
// "warning" (never a build_blocker — those apps must keep working) and the
// analytics endpoint attaches it as c.freelance. At-risk-shaped = a chat
// trigger runs a bot-local script; if the script fails the agent could leak a
// raw "(agent) failed" line into chat OR confabulate a fake success.
//
// Since the execution-integrity harness (#3362) shipped fleet-wide, that
// INTEGRITY risk is removed for any app whose script the harness recognizes —
// the middleware substitutes an honest failure result before the model sees it.
// So the badge is now HARNESS-AWARE: it clears for a proven-covered app
// (c.freelance.integrity_covered) and fires only on the residual gap — an
// at-risk app the harness does not yet wrap. Conservative: only proven coverage
// clears; unknown keeps the badge (falsely clearing is the dishonesty this arc
// fights). Clicking still opens the reliability modal, but that modal no longer
// sells plugin_intercept as the universal misreport cure — honesty is automatic
// now; the migration is narrowed to genuinely event/channel-shaped apps.
// Warning-tinted (var(--yellow), style-guide §9.5); renders nothing unless the
// server attached a warning.
function _freelanceMigrateChip(c) {
  const f = c.freelance;
  if (!f) return '';
  // Harness-aware clear (Bite 2a, spec-app-invocation-just-works §2.2/§3). When
  // the execution-integrity middleware PROVABLY covers this app's script (the
  // server proved it against the plugin's coverage file), an honest failure
  // result is substituted pre-model — the "may misreport" condition is false by
  // construction, so the badge disappears. Conservative by contract: the server
  // sets this true ONLY on proven coverage; a covered==unknown app keeps the
  // badge. This is the visible clear point for the load-bearing safety property.
  if (f.integrity_covered) return '';
  // Fire for an at-risk warning OR an app that can actually be migrated. The
  // server keeps `severity` honest (the validator's own value); the chip is
  // the union so the action surfaces wherever it's useful.
  if (f.severity !== 'warning' && !f.can_migrate) return '';
  const tip = 'This app may misreport failures — click to see why and make it reliable.';
  const bot = _capBot || '';
  const canMig = (f.can_migrate ? 'true' : 'false');
  // attrJsLiteral encodes the value as a quoted JS literal with " → &quot; so
  // operator-authored names with apostrophes/quotes/backslashes can't break
  // the onclick attribute (the inert-button bug). No hand-rolled quoting.
  const onclick = `onclick="openReliabilityModal(${attrJsLiteral(bot)},${attrJsLiteral(c.id)},${attrJsLiteral(c.name || '')},${canMig})"`;
  return `<span class="badge badge-inline" style="font-size:0.65rem;background:rgba(245,158,11,0.12);color:var(--yellow);border:1px solid rgba(245,158,11,0.3);cursor:pointer" title="${escHtml(tip)}" ${onclick}>⚠ May misreport failures</span>`;
}

// ── Defined / Discovered provenance + drift narrative (spec §9, Bite 4) ──────
// The source-of-truth axis (spec §9.1): `discovered` = the scanner's synthesis
// from observed workspace files — a churnable draft Evolve guessed at;
// `defined` = the operator has VOUCHED for it, so the scanner may only observe
// and narrate, never merge / rename / auto-remove. The chip is subtle
// provenance, not an alert: neutral grey for discovered, a quiet green check
// for defined. Token colors only (var(--bg4)/var(--text3)/var(--green)),
// badge-inline inline-style pattern (style-guide §9.5). Jargon-free tooltips.
function _definitionChip(c) {
  const ds = c.definition_status || 'discovered';
  if (ds === 'defined') {
    const tip = "You've vouched for this app — the scanner won't merge, rename, or auto-remove it.";
    return `<span class="badge badge-inline" style="font-size:0.65rem;background:var(--bg4);color:var(--green)" title="${escHtml(tip)}">✓ defined</span>`;
  }
  const tip = "Inferred from your bot's workspace by the scanner. Promote it to vouch this app is real.";
  return `<span class="badge badge-inline" style="font-size:0.65rem;background:var(--bg4);color:var(--text3)" title="${escHtml(tip)}">discovered</span>`;
}

// Unreviewed-drift badge (spec §9.3). Fires ONLY for a `defined` app with
// unreviewed MAJOR drift_log entries — a `discovered` app accrues no narrative
// (narrate_drift is a no-op for it), so the badge is double-gated on
// definition_status AND a positive count. Clicking opens the drift panel.
function _driftReviewPill(c) {
  if ((c.definition_status || 'discovered') !== 'defined') return '';
  const n = c.unreviewed_drift_count || 0;
  if (n <= 0) return '';
  const tip = `${n} significant change${n === 1 ? '' : 's'} since you last reviewed this app. Click to see what changed.`;
  const stem = c.manifest_stem || c.id;
  const onclick = `onclick="openDriftPanel(${attrJsLiteral(_capBot || '')},${attrJsLiteral(stem)},${attrJsLiteral(c.name || '')})"`;
  return `<span class="badge badge-inline" style="font-size:0.65rem;background:var(--bg4);color:var(--blue);cursor:pointer" title="${escHtml(tip)}" ${onclick}>↻ ${n} unreviewed change${n === 1 ? '' : 's'}</span>`;
}

// ── Promote / Demote to defined (spec §9.6 bite 4) ──────────────────────────
// Operator vouches (or un-vouches) the app's identity/existence. CRITICAL: the
// definition endpoints write back by FILENAME STEM, which can differ from the
// internal id on gallery v7-arc-pre apps (§9.7 finding #5) — callers pass
// `manifest_stem`, never `c.id`. Confirm-first via the global confirmModal
// (native confirm() is silently dropped in the desktop webview); toast results.
// Distinct from "Make reliable" (failure-reporting migration) — these only
// vouch identity; the two are cross-linked, never bundled (operator decision).
async function promoteDefinition(botId, stem, name) {
  const nm = name || stem;
  if (!await confirmModal({
    title: 'Promote to defined?',
    body: `Vouch for "${nm}" as a real app.\n\nThe scanner keeps its details current, but will no longer merge, rename, or auto-remove it — even if its files go missing. You can demote it again at any time.`,
    confirmLabel: 'Promote',
  })) return;
  const r = await api('POST', `/api/applications/${encodeURIComponent(botId)}/${encodeURIComponent(stem)}/definition/promote`);
  if (r && r.ok) {
    toast('✓ Promoted to defined', 'ok');
    await _afterDefinitionChange(botId, stem);
  } else {
    toast('✗ ' + ((r && r.error) || 'Promote failed'), 'err');
  }
}

async function demoteDefinition(botId, stem, name) {
  const nm = name || stem;
  if (!await confirmModal({
    title: 'Demote to discovered?',
    body: `Un-vouch "${nm}".\n\nThe scanner will manage it as a draft again — free to merge, rename, or auto-remove it if its files disappear. Reversible: you can promote it again later.`,
    confirmLabel: 'Demote',
  })) return;
  const r = await api('POST', `/api/applications/${encodeURIComponent(botId)}/${encodeURIComponent(stem)}/definition/demote`);
  if (r && r.ok) {
    toast('✓ Demoted to discovered', 'ok');
    await _afterDefinitionChange(botId, stem);
  } else {
    toast('✗ ' + ((r && r.error) || 'Demote failed'), 'err');
  }
}

// Refresh the grid + (if open on this app) the detail drawer after a vouch flip.
async function _afterDefinitionChange(botId, stem) {
  if (typeof loadCapabilities === 'function') await loadCapabilities();
  if (_mBotId === botId && (_mStem === stem || _mAppId === stem)) {
    await viewManifest(_mBotId, _mAppId, _mStem);
  }
}

// ── Drift panel — list the narrated changes + "Mark all reviewed" ───────────
let _driftBot = null, _driftStem = null, _driftName = '', _driftEntries = [];

async function openDriftPanel(botId, stem, name) {
  _driftBot = botId; _driftStem = stem; _driftName = name || stem;
  // Always fetch fresh so the list reflects the latest reviewed flags (the
  // GET resolves stem OR internal id, so either identifier lands on the file).
  const m = await api('GET', `/api/applications/${encodeURIComponent(botId)}/${encodeURIComponent(stem)}`);
  if (m.error) { toast('✗ ' + m.error, 'err'); return; }
  _driftEntries = (m.drift_log || []).filter(e => e && e.source === 'scanner_drift');
  _renderDriftPanel();
  document.getElementById('drift-panel-modal').classList.add('open');
}

function _renderDriftPanel() {
  const body = document.getElementById('drift-panel-body');
  if (!body) return;
  const esc = escHtml;
  const entries = (_driftEntries || []).slice().sort(
    (a, b) => String(b.ts || '').localeCompare(String(a.ts || '')));
  const unrev = entries.filter(e => e.reviewed === false).length;
  const kindLabel = { add: '＋ added', remove: '－ removed', modify: '~ changed' };
  const rows = entries.length ? entries.map(e => {
    const reviewed = e.reviewed !== false;
    const kl = kindLabel[e.kind] || esc(e.kind || 'changed');
    const when = esc(String(e.ts || '').slice(0, 10));
    return `<div style="padding:8px 0;border-bottom:1px solid var(--border)${reviewed ? ';opacity:0.55' : ''}">
      <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap">
        <span style="font-size:0.72rem;color:var(--text3);white-space:nowrap">${when}</span>
        <span style="font-size:0.7rem;color:var(--text2)">${kl} · ${esc(e.target_type || '')}</span>
        ${reviewed ? `<span class="badge badge-inline" style="font-size:0.6rem;background:var(--bg4);color:var(--green)">✓ reviewed</span>` : ''}
      </div>
      <div style="font-size:0.82rem;color:var(--text1);margin-top:2px">${esc(e.summary || e.target || '')}</div>
    </div>`;
  }).join('') : `<div style="font-size:0.82rem;color:var(--text3)">No tracked changes.</div>`;
  body.innerHTML = `<div style="font-size:1rem;font-weight:700;margin-bottom:4px">Changes — ${esc(_driftName)}</div>
    <div style="font-size:0.78rem;color:var(--text2);margin-bottom:12px">Significant changes the scanner absorbed since you vouched for this app. Review them, then mark them seen.</div>
    <div>${rows}</div>`;
  const btn = document.getElementById('drift-panel-review-btn');
  if (btn) {
    btn.disabled = unrev === 0;
    btn.textContent = unrev > 0 ? `Mark all reviewed (${unrev})` : 'All reviewed';
  }
}

async function markDriftReviewed() {
  if (!_driftBot || !_driftStem) return;
  const btn = document.getElementById('drift-panel-review-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Working…'; }
  const r = await api('POST', `/api/applications/${encodeURIComponent(_driftBot)}/${encodeURIComponent(_driftStem)}/definition/drift/review`, { reviewed: true });
  if (r && r.ok) {
    toast(`✓ Marked ${r.updated || 0} change${r.updated === 1 ? '' : 's'} reviewed`, 'ok');
    _driftEntries = (_driftEntries || []).map(e => ({ ...e, reviewed: true }));
    _renderDriftPanel();
    if (typeof loadCapabilities === 'function') loadCapabilities();
  } else {
    toast('✗ ' + ((r && r.error) || 'Could not mark reviewed'), 'err');
    if (btn) { btn.disabled = false; btn.textContent = 'Mark all reviewed'; }
  }
}

function closeDriftPanel() {
  document.getElementById('drift-panel-modal')?.classList.remove('open');
  _driftBot = null; _driftStem = null; _driftName = ''; _driftEntries = [];
}

// onclick-string handlers → globals (no-unused-vars; classic-script globals).
window._definitionChip = _definitionChip;
window._driftReviewPill = _driftReviewPill;
window.promoteDefinition = promoteDefinition;
window.demoteDefinition = demoteDefinition;
window.openDriftPanel = openDriftPanel;
window.markDriftReviewed = markDriftReviewed;
window.closeDriftPanel = closeDriftPanel;

// Modal state — which (bot, app) the "Make reliable" action targets, and
// whether a one-click migration is actually possible for it.
let _relBot = null, _relApp = null, _relName = '', _relCanMigrate = false;

// Jargon-free explainer, narrowed by Bite 2a (spec-app-invocation-just-works
// §2.3/§3). This action is NO LONGER pitched as the universal cure for
// "misreporting": the execution-integrity harness now captures real script
// failures automatically, regardless of how the app is invoked, so honesty
// doesn't depend on flipping a mode. What this action IS for is the recognition
// axis — turning a genuinely EVENT/CHANNEL-shaped app into a deterministic
// hook. So the copy leads with "run automatically on a fixed trigger," reserves
// it for event-shaped apps, and reassures that failure reporting is handled
// either way. The implementation terms (plugin_intercept / agent_invokes)
// appear only in the dimmed technical note. When the app can't be one-click
// migrated (no declared message triggers), the body explains that honestly
// instead of offering a button that would just fail.
function _reliabilityExplainerHtml() {
  const name = escHtml(_relName || 'this app');
  const lead = `<div style="font-size:0.95rem;font-weight:600;margin-bottom:10px">Run "${name}" automatically</div>
    <div style="font-size:0.85rem;color:var(--text2);line-height:1.6">
      <p style="margin:0 0 8px">By default the bot's AI decides when to run this app and calls its
        script itself — the natural way to use an app, and the system already
        <strong style="color:var(--text)">reports the real outcome if the script fails</strong>, so it
        can't leak a raw error or invent a success.</p>
      <p style="margin:0 0 8px">This option is for a different case: if this app is a
        <strong style="color:var(--text)">true event or channel hook</strong> — a message that should
        <em>always</em> run its script the same way, not an open-ended request the AI interprets — you
        can switch it to run that script <strong style="color:var(--text)">directly and deterministically</strong>
        on those messages.</p>`;

  if (!_relCanMigrate) {
    return lead + `</div>
      <div style="margin-top:14px;padding:12px 14px;border:1px solid var(--border);border-radius:8px;background:var(--bg3)">
        <div style="font-size:0.82rem;color:var(--text2);line-height:1.55">This app can't be switched
          automatically yet: it hasn't declared <strong style="color:var(--text)">which messages should
          trigger it</strong>. That's only needed if it's genuinely event-shaped — for a normal
          "just ask for it" app, leave it as-is; failures are reported honestly either way.</div>
      </div>
      <div style="font-size:0.72rem;color:var(--text3);margin-top:12px;border-top:1px solid var(--border);padding-top:8px">
        Technical note: deterministic mode (<code>plugin_intercept</code>) runs each declared message
        trigger's script directly. Without structured <code>event_triggers</code> there's nothing to
        wire. Failure honesty is handled separately by the execution-integrity harness.
      </div>`;
  }

  return lead + `
      <p style="margin:0">Best for fixed, event-shaped triggers. For a normal "just ask for it" app,
        leaving it as-is keeps the AI free to pick the right app — and failures are reported honestly
        either way.</p>
    </div>
    <div style="font-size:0.72rem;color:var(--text3);margin-top:12px;border-top:1px solid var(--border);padding-top:8px">
      Technical note: this switches the app's <code>invocation_mode</code> from
      <code>agent_invokes</code> to <code>plugin_intercept</code> and wires each message trigger to
      run its script deterministically. You can review the manifest afterward.
    </div>`;
}

function _renderReliabilityModalBody(extraHtml) {
  const el = document.getElementById('reliability-modal-body');
  if (el) el.innerHTML = _reliabilityExplainerHtml() + (extraHtml || '');
}

function openReliabilityModal(botId, appId, appName, canMigrate) {
  _relBot = botId; _relApp = appId; _relName = appName || appId; _relCanMigrate = !!canMigrate;
  _renderReliabilityModalBody('');
  const btn = document.getElementById('reliability-apply-btn');
  if (btn) {
    if (_relCanMigrate) { btn.style.display = ''; btn.disabled = false; btn.textContent = 'Switch to automatic mode'; }
    else { btn.style.display = 'none'; }  // no action offered when it can't be done
  }
  document.getElementById('reliability-modal').classList.add('open');
}

function closeReliabilityModal() {
  document.getElementById('reliability-modal')?.classList.remove('open');
}

// Confirm-first, then re-forge the installed manifest to plugin_intercept.
// Handled "can't be made reliable" outcomes (HTTP 200, ok:false) render their
// reason inline and hide the action — they're deterministic, retrying the same
// manifest won't help. A genuine network failure keeps the button for retry.
async function makeAppReliable() {
  if (!_relBot || !_relApp) return;
  if (!await confirmModal(`Switch "${_relName}" to automatic mode?\n\nThis makes a declared message run the app's script directly, every time, instead of the AI deciding when to call it — best for a true event or channel hook. The app keeps doing what it does today; you can review the manifest afterward.`)) return;

  const btn = document.getElementById('reliability-apply-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Working…'; }

  const r = await api('POST', `/api/applications/${_relBot}/${_relApp}/make-reliable`);

  if (r && r.ok) {
    toast(r.changed ? '✓ App now runs automatically' : 'Already in automatic mode', 'ok');
    closeReliabilityModal();
    await loadCapabilities();
    return;
  }

  // Failure: show the reason inline, keep the modal open.
  const transient = !r || r.network_error;
  const reason = (r && (r.reason || r.error)) || 'Could not reach the server. Please try again.';
  _renderReliabilityModalBody(`<div style="margin-top:14px;padding:12px 14px;border:1px solid var(--border);border-radius:8px;background:var(--bg3)">
      <div style="font-size:0.78rem;font-weight:600;color:var(--yellow);margin-bottom:6px">Couldn't switch this app to automatic mode yet</div>
      <div style="font-size:0.82rem;color:var(--text2);line-height:1.55">${escHtml(reason)}</div>
    </div>`);
  if (btn) {
    if (transient) { btn.disabled = false; btn.textContent = 'Switch to automatic mode'; }
    else { btn.style.display = 'none'; }
  }
}

// Expose the onclick-string handlers as globals (no-undef is off, but
// no-unused-vars flags functions only referenced from HTML onclick strings).
window.openReliabilityModal = openReliabilityModal;
window.closeReliabilityModal = closeReliabilityModal;
window.makeAppReliable = makeAppReliable;

// ── Honest activity reporting (audit 2026-06-13) ────────────────────────────
// Two distinct signals, never conflated:
//   1. Delivery status (PRIMARY) — the delivery_monitor ledger's most-recent
//      classified outcome: did this app's scheduled, user-facing delivery
//      actually reach the user? This is real usage.
//   2. File footprint (SECONDARY, muted) — usage_logger's evidence-file mtime
//      sweep. A maintenance signal ("was anything touched lately?"), NOT
//      usage. The old "Active / Quiet / Inactive" verdicts derived from it
//      were a file-mtime-as-usage proxy and are removed.

// Phase 4.5 partial-materialize warning (audit slate S2, 2026-07-02). Lit
// when one or more of the app's scheduled_actions[] carry install_error —
// forge shipped the app but those schedules never went live, so they will
// never fire until remediated. Without this line a partially-materialized
// app is indistinguishable from a healthy one on the card (the delivery
// monitor only catches user-facing schedules, and only after a missed
// window). Returns '' when everything materialized.
function _scheduleNotLiveLine(c) {
  const n = c.scheduled_install_errors || 0;
  if (!n) return '';
  const word = n === 1 ? 'scheduled task' : 'scheduled tasks';
  const tip = 'The app installed, but the automatic setup of '
    + (n === 1 ? 'this scheduled task' : 'these scheduled tasks')
    + ' failed — they will not run until re-installed. See the install job '
    + 'on the Forge Jobs page (or the Alerts page) for the exact error, '
    + 'then retry the install.';
  return `<div style="font-size:0.72rem;color:var(--yellow);font-weight:600;margin-bottom:6px" title="${escHtml(tip)}">⚠ ${n} ${word} failed to set up — not running</div>`;
}

// PRIMARY delivery line. Returns '' only when there is genuinely nothing
// honest to say (handled by the caller falling through to the footprint).
function _deliveryStatusLine(c) {
  const d = c.delivery; // {outcome, diagnosis, suspected_cause, last_event_ts} | null
  const muted = 'color:var(--text3)';
  if (!d || !d.outcome) {
    // No delivery measurement exists. Be honest about why instead of
    // inferring activity from file edits.
    const msg = c.has_scheduled_actions
      ? 'Delivery status not yet recorded'
      : 'Usage not measured';
    const tip = c.has_scheduled_actions
      ? 'This app has a scheduled delivery but the delivery monitor has not classified a window yet (fresh install, or a non-user-facing schedule).'
      : 'This app runs on demand, not on a schedule — there is no automatic delivery signal to measure. File activity below is a maintenance footprint, not usage.';
    return `<div style="font-size:0.72rem;${muted};margin-bottom:6px" title="${escHtml(tip)}">${msg}</div>`;
  }

  const agoStr = d.last_event_ts ? ago(d.last_event_ts) : '';
  const CAUSE_NOTE = {
    host_asleep: 'the computer was asleep',
    dst: 'a daylight-saving clock change overlapped',
    not_loaded: 'the scheduled job was not loaded',
    script_error: 'the app hit an error while running',
    gateway_down: 'the messaging connection was down',
  };
  let color, label, tip = '';
  switch (d.outcome) {
    case 'on_time':
      color = 'var(--green)';
      label = `✓ Delivered on time${agoStr ? ' · last run ' + agoStr : ''}`;
      break;
    case 'late':
      color = 'var(--yellow)';
      label = `✓ Delivered late${agoStr ? ' · last run ' + agoStr : ''}`;
      tip = 'The delivery arrived, but after its scheduled window.';
      break;
    case 'missed':
      if (d.diagnosis === 'ran_undelivered') {
        color = 'var(--yellow)';
        label = `⚠ Ran — delivery unconfirmed${agoStr ? ' · ' + agoStr : ''}`;
        tip = 'The app ran but there is no proof the message reached the user.';
      } else {
        color = 'var(--red)';
        label = `⚠ Did not run${agoStr ? ' · last due ' + agoStr : ''}`;
        tip = 'The scheduled delivery did not fire in its window.';
      }
      if (d.suspected_cause && CAUSE_NOTE[d.suspected_cause]) {
        tip += ` Suspected cause: ${CAUSE_NOTE[d.suspected_cause]}.`;
      }
      break;
    case 'unmeasurable':
      color = 'var(--text3)';
      label = 'Delivery unmeasurable';
      tip = 'The monitor could not gather the evidence it needs to confirm or deny this delivery (e.g. a log it cannot read). Never reported as a false OK.';
      break;
    case 'unmonitorable':
      color = 'var(--text3)';
      label = 'Delivery not tracked for this schedule';
      tip = "This app's schedule type has no deterministic delivery proof, so it is not monitored — shown honestly rather than as a fake green.";
      break;
    case 'disabled':
      color = 'var(--text3)';
      label = 'Scheduled delivery paused';
      break;
    default:
      color = 'var(--text3)';
      label = 'Delivery status unknown';
  }
  const titleAttr = tip ? ` title="${escHtml(tip)}"` : '';
  return `<div style="font-size:0.72rem;color:${color};font-weight:600;margin-bottom:6px"${titleAttr}>${label}</div>`;
}

// SECONDARY footprint line — file-modification maintenance signal, muted.
// Never an Active/Quiet/Inactive usage verdict (those were removed in the
// 2026-06-13 honest-usage-reporting audit). Returns '' when there is no
// footprint data to show.
function _footprintLine(c) {
  const lastModified    = c.usage_last_modified_ts;
  const active30        = c.usage_active_files_30d;
  const totalFiles      = c.usage_total_files;
  const evidencePresent = c.usage_evidence_present;
  const manifestFiles   = c.files_count;
  const hasNonFileActivity = !!(c.has_crons || c.has_scheduled_actions
                                 || c.has_heartbeat_evidence);
  const drivenByLabel = c.has_crons ? 'cron'
                        : c.has_heartbeat_evidence ? 'heartbeat'
                        : c.has_scheduled_actions ? 'scheduled actions'
                        : '';
  const muted = 'color:var(--text3)';

  // usage_logger hasn't run since this manifest was created/updated.
  if (totalFiles == null && manifestFiles != null) {
    if (manifestFiles > 0) {
      const w = manifestFiles === 1 ? 'file' : 'files';
      return `<div style="font-size:0.72rem;${muted};margin-bottom:4px">${manifestFiles} ${w} tracked · footprint scan pending</div>`;
    }
    if (hasNonFileActivity) {
      return `<div style="font-size:0.72rem;${muted};margin-bottom:4px">${drivenByLabel}-driven · footprint scan pending</div>`;
    }
    return `<div style="font-size:0.72rem;color:var(--yellow);margin-bottom:4px">⚠️ No evidence files</div>`;
  }
  if (totalFiles == null) return ''; // no footprint data at all

  const trulyFileless = !evidencePresent || (totalFiles === 0 && !manifestFiles);
  if (trulyFileless && !hasNonFileActivity) {
    return `<div style="font-size:0.72rem;color:var(--yellow);margin-bottom:4px">⚠️ No evidence files</div>`;
  }
  if (trulyFileless && hasNonFileActivity) {
    return `<div style="font-size:0.72rem;${muted};margin-bottom:4px">${drivenByLabel}-driven</div>`;
  }
  if (totalFiles === 0 && manifestFiles > 0) {
    return `<div style="font-size:0.72rem;color:var(--yellow);margin-bottom:4px">⚠️ Registered files missing on disk</div>`;
  }

  // Honest footprint: change-count + tracked-count + last-touched. No
  // verdict word — this is a maintenance signal, not a usage claim.
  const changed = active30 || 0;
  const cWord = changed === 1 ? 'file' : 'files';
  const tWord = totalFiles === 1 ? 'file' : 'files';
  let label = `${changed} ${cWord} changed in 30d · ${totalFiles} ${tWord} tracked`;
  if (lastModified) label += ` · last touched ${ago(lastModified)}`;
  const tip = 'File-modification footprint (a maintenance signal — whether the app\'s files were edited recently). Not a measure of whether the app delivered to the user.';
  return `<div style="font-size:0.72rem;${muted};margin-bottom:4px" title="${escHtml(tip)}">${label}</div>`;
}

// Open the manifest modal and scroll to the Coherence + Drift section
// once the DOM is rendered.
async function _openCoherenceModal(botId, appId) {
  await viewManifest(botId, appId);
  setTimeout(() => {
    const sec = document.getElementById('m-coherence-section');
    if (sec && sec.scrollIntoView) sec.scrollIntoView({behavior: 'smooth', block: 'start'});
  }, 80);
}

// Lifecycle action buttons for one app/capability (View / Pause / Share /
// Promote / Archive / Uninstall / Restore). Shared by the applications grid
// cards and the capabilities affordance below — a capability is real installed
// code, so its lifecycle controls are identical to an application's.
function _capActionBtns(c, bot) {
  const isArchived = c.status === 'hidden' || c.status === 'dormant';
  const isPaused   = c.status === 'paused';
  // The definition endpoints key off the FILENAME STEM, which can differ from
  // the internal id on gallery v7-arc-pre apps (§9.7 finding #5). View also
  // gets the stem so the drawer's definition control hits the right file.
  const defStem = c.manifest_stem || c.id;
  let actionBtns = `<button class="btn btn-ghost btn-sm" onclick="viewManifest('${bot}','${escHtml(c.id)}','${escHtml(defStem)}')">View</button>`;
  if (isArchived) {
    actionBtns += ` <button class="btn btn-ghost btn-sm" onclick="restoreApp('${bot}','${escHtml(c.id)}')">Restore</button>`;
  } else {
    if (isPaused) {
      actionBtns += ` <button class="btn btn-ghost btn-sm" onclick="unpauseApp('${bot}','${escHtml(c.id)}')">▶ Unpause</button>`;
    } else if (c.has_crons) {
      actionBtns += ` <button class="btn btn-ghost btn-sm" onclick="pauseApp('${bot}','${escHtml(c.id)}')">⏸ Pause</button>`;
    }
    // Share — distill into a pod-local Spec, optionally install onto another bot.
    actionBtns += ` <button class="btn btn-ghost btn-sm" onclick="openShareModal('${bot}','${escHtml(c.id)}','${escHtml(c.name||'')}')">⇄ Share</button>`;
    // Promote / Demote to defined (spec §9.6 bite 4) — the operator's identity
    // vouch. The word "Promote" now means THIS (definition-promote); the
    // gallery files-pack export below was renamed "Export to Gallery" to clear
    // the collision (spec §9.6 bite 1 / bite 4 #4). Distinct from the coherence
    // modal's "Promote to authored" (a different surface/vocabulary).
    if ((c.definition_status || 'discovered') === 'defined') {
      actionBtns += ` <button class="btn btn-ghost btn-sm" onclick="demoteDefinition(${attrJsLiteral(bot)},${attrJsLiteral(defStem)},${attrJsLiteral(c.name||'')})" title="Un-vouch — let the scanner manage this app as a draft again (reversible)">Demote to discovered</button>`;
    } else {
      actionBtns += ` <button class="btn btn-ghost btn-sm" onclick="promoteDefinition(${attrJsLiteral(bot)},${attrJsLiteral(defStem)},${attrJsLiteral(c.name||'')})" title="Vouch for this app — the scanner won't merge, rename, or auto-remove it">Promote to defined</button>`;
    }
    // Export to Gallery — snapshot this install into a gallery files-pack
    // candidate for review + commit. F-P.7.b; needs the package's pkg_id.
    // RENAMED from "↥ Promote" (spec §9.6 bite 4 #4) so "Promote" is
    // unambiguously the definition-promote above. The handler/endpoint
    // (openPromoteModal → /api/gallery/promote/snapshot) is unchanged.
    if (c.pkg_id) {
      actionBtns += ` <button class="btn btn-ghost btn-sm" onclick="openPromoteModal('${bot}','${escHtml(c.pkg_id)}','${escHtml(c.name||'')}')" title="Export this install to the gallery as a files-pack — cheap-install for future deploys">Export to Gallery</button>`;
    }
    actionBtns += ` <button class="btn btn-ghost btn-sm" onclick="archiveApp('${bot}','${escHtml(c.id)}')">Archive</button>`;
    actionBtns += ` <button class="btn btn-ghost btn-sm" style="color:var(--red)" onclick="openUninstallWizard('${bot}','${escHtml(c.id)}')">Uninstall…</button>`;
  }
  return actionBtns;
}

function renderCapabilities() {
  _renderCapTestRollup();
  const statusFilter  = document.getElementById('cap-status')?.value || '';
  const health        = document.getElementById('cap-health')?.value || '';
  const showArchived  = document.getElementById('cap-show-archived')?.checked || false;
  let caps = _capData;

  // Default: hide archived (hidden/dormant) unless the toggle is on or they're explicitly selected
  if (!showArchived && statusFilter !== 'hidden' && statusFilter !== 'dormant' && statusFilter !== 'all') {
    caps = caps.filter(c => c.status !== 'hidden' && c.status !== 'dormant');
  }

  // Status filter
  if (statusFilter && statusFilter !== 'all') {
    caps = caps.filter(c => c.status === statusFilter);
  }

  // Test-health filter removed 2026-06-08; the underlying surface is gone.
  // The `cap-health` select drops to a no-op if the operator still has the
  // dropdown rendered from a cached page.
  const el = document.getElementById('cap-grid');
  if (_capData.length === 0) {
    el.innerHTML = `<div class="empty-state-card">
      <div style="font-size:2rem;margin-bottom:12px">📱</div>
      <div style="font-weight:600;margin-bottom:8px">No applications discovered yet</div>
      <div style="color:var(--text2);font-size:0.82rem;margin-bottom:16px;max-width:400px;margin-left:auto;margin-right:auto">
        Applications are features your bot actually has — detected from files in its workspace.
        Auto-discovery checks for evidence of real use.
      </div>
      <button class="btn btn-primary" onclick="runCapabilityScan()">🔍 Full scan (AI-powered, ~1 min)</button>
      <span style="color:var(--text3);font-size:0.78rem;margin:0 8px">or</span>
      <button class="btn btn-ghost" onclick="runCapabilityScan(true)">⚡ Quick scan (evidence only, ~5s)</button>
      <span style="color:var(--text3);font-size:0.78rem;margin:0 8px">or</span>
      <button class="btn btn-ghost" onclick="showNewCapabilityForm()">+ Create manually</button>
    </div>`;
    _renderCapabilitiesAffordance([], _capBot || '');  // clear any stale section
    _renderSystemAffordance([], _capBot || '');
    return;
  }
  const bot = _capBot || '';
  // ── Route non-application manifests off the applications grid ───────────────
  // The scanner labels each manifest app_kind = "application" | "capability" |
  // "system" (purpose/fit classifier). Two kinds are not goal-shaped user
  // applications and so don't belong in the headline grid:
  //   • "capability" (PR #2899) — a reusable skill (e.g. Google API access)
  //   • "system"     (PR #3060) — pod/agent-runtime infrastructure
  //                               (self-healing, heartbeat, scheduling, the
  //                               pod's own telemetry)
  // INERT DEFAULT "application" (load-bearing over-route guard): a manifest
  // without the field (legacy / un-classified) or with ANY unrecognized value
  // stays in the grid — only an exact "capability"/"system" routes off, so the
  // over-route direction (hiding a real app) never happens. Split AFTER the
  // status filter so all surfaces share filter state. The routed-off manifests
  // still render below, each with full lifecycle controls, in their own
  // subordinate affordance.
  const isCapability = (c) => (c.app_kind || 'application') === 'capability';
  const isSystem     = (c) => (c.app_kind || 'application') === 'system';
  const capabilities = caps.filter(isCapability);
  const systemFns    = caps.filter(isSystem);
  const apps = caps.filter(c => !isCapability(c) && !isSystem(c));
  _renderCapabilitiesAffordance(capabilities, bot);
  _renderSystemAffordance(systemFns, bot);
  if (!apps.length) {
    el.innerHTML = '<div class="empty">No applications match filters.</div>';
    return;
  }
  const countNote = apps.length > 6
    ? `<div style="font-size:0.78rem;color:var(--text2);margin-bottom:10px;grid-column:1/-1">Showing ${apps.length} applications. Use filters to narrow.</div>`
    : '';
  el.innerHTML = countNote + apps.map(c => {
    const confPct = c.confidence != null ? Math.round(c.confidence * 100) + '%' : null;
    const evFiles = (c.evidence_files || []).slice(0, 3).join(', ');

    // ── Activity reporting (honest-usage audit 2026-06-13) ──────────────────
    // PRIMARY = real delivery status (delivery_monitor ledger). SECONDARY =
    // file-mtime footprint (maintenance signal, never a usage verdict).
    const deliveryLine  = _deliveryStatusLine(c);
    const footprintLine = _footprintLine(c);
    const schedNotLiveLine = _scheduleNotLiveLine(c);

    // ── Status badge ──────────────────────────────────────────────────────────
    const STATUS_BADGE = {
      paused:     `<span class="badge badge-inline" style="font-size:0.65rem">⏸ paused</span>`,
      hidden:     `<span class="badge" style="font-size:0.65rem;background:var(--bg4);color:var(--text3)">📦 archived</span>`,
      dormant:    `<span class="badge" style="font-size:0.65rem;background:var(--bg4);color:var(--text3)">💤 dormant</span>`,
      draft:      `<span class="badge badge-inline" style="font-size:0.65rem">draft</span>`,
      deprecated: `<span class="badge" style="font-size:0.65rem;background:var(--bg4);color:var(--text3)">deprecated</span>`,
    };
    const statusBadge = STATUS_BADGE[c.status] || '';

    // ── Action buttons ────────────────────────────────────────────────────────
    const isArchived = c.status === 'hidden' || c.status === 'dormant';
    const actionBtns = _capActionBtns(c, bot);

    // ── Coherence + Drift chips (spec-app-coherence-and-reconciliation §10) ──
    // Lit by the new analytics fields populated in server.py
    // (coherence_status / reconciliation_status / *_count). Pre-coherence
    // manifests come back as status=ok and no chip renders — same as the
    // existing pattern for empty pills.
    const coherencePill = _coherenceChip(c);
    const reconciliationPill = _reconciliationChip(c);

    // ── Agent-freelance migrate nudge (spec-agent-freelance-bypass-phase2) ───
    // Lit only for at-risk-shaped manifests still in agent_invokes mode (the
    // server attaches c.freelance with severity "warning"). Clicking opens the
    // manifest modal.
    const freelancePill = _freelanceMigrateChip(c);

    // ── Defined/Discovered provenance + unreviewed-drift badge (spec §9) ─────
    // definitionPill is always shown (provenance); driftReviewPill only for a
    // defined app with unreviewed major drift.
    const definitionPill = _definitionChip(c);
    const driftReviewPill = _driftReviewPill(c);

    // ── Spec drift badge (S3c) ───────────────────────────────────────────────
    // Surfaced when an Instance is pinned to an older Spec version than what's
    // in the gallery. Hides for 'none' and 'unknown' (the latter covers v13
    // legacy manifests and Instances missing provenance). 'downgrade' is rare
    // enough that we skip the badge — operators can find it in View Manifest.
    let driftPill = '';
    if (c.spec_drift_kind === 'drift') {
      const behind = c.spec_versions_behind || 1;
      const tip = `Newer Spec version ${c.latest_spec_version || ''} available `
        + `(current: ${c.current_spec_version || '?'}). Click to review + adopt.`;
      // Clickable: opens the Adopt review modal (Adopt v1, S8.1.5).
      driftPill = `<span class="badge badge-inline" style="font-size:0.65rem;background:var(--bg4);color:var(--blue);cursor:pointer" title="${escHtml(tip)}" onclick="openAdoptModal('${escHtml(bot)}','${escHtml(c.id)}','${escHtml(c.name||'')}')">🔄 update available${behind > 1 ? ' (+' + behind + ')' : ''}</span>`;
    } else if (c.spec_drift_kind === 'spec_missing') {
      const tip = `Instance pinned to ${c.current_spec_version || '?'} but no Spec exists in the gallery for this app. Adopt is blocked until the Spec is restored.`;
      driftPill = `<span class="badge badge-inline" style="font-size:0.65rem;background:var(--bg4);color:var(--red)" title="${escHtml(tip)}">⚠ spec missing</span>`;
    }

    return `<div class="cap-card" style="${isArchived ? 'opacity:0.65' : ''}">
      <div class="cap-card-head">
        <div class="cap-card-name" title="${c.name}">${escHtml(c.name)}</div>
        ${statusBadge}
        ${definitionPill}
        ${driftReviewPill}
        ${driftPill}
        ${coherencePill}
        ${reconciliationPill}
        ${freelancePill}
      </div>
      ${c.description ? `<div title="${escHtml(c.description)}" style="font-size:0.78rem;color:var(--text2);margin-bottom:6px;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden">${escHtml(c.description)}</div>` : ''}
      <div style="margin-bottom:6px">${stars(c.satisfaction_score)}</div>
      ${schedNotLiveLine}
      ${deliveryLine}
      ${footprintLine}
      ${confPct ? `<div style="font-size:0.72rem;color:var(--text3);margin-bottom:4px">Confidence: ${confPct}</div>` : ''}
      ${evFiles ? `<div style="font-size:0.72rem;color:var(--text3);margin-bottom:6px">Evidence: ${evFiles}</div>` : ''}
      <div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:5px">${actionBtns}</div>
    </div>`;
  }).join('');
}

// ── Routed-off affordances (D2 / Scanner Slice 2) ──────────────────────────
// Manifests the scanner routed off the headline applications grid still need a
// home — they're real installed code with full lifecycle controls, just not
// goal-shaped user applications. Two kinds get routed off:
//   • app_kind="capability" (purpose/fit classifier, PR #2899) — reusable skill
//   • app_kind="system"     (Scanner Slice 2, PR #3060)        — pod/agent
//                            runtime infrastructure
// Each renders the same subordinate, keyboard-operable <details>: a labeled
// chevron summary + each entry's name/objective + its lifecycle controls (View /
// Pause / Share / Promote / Archive / Uninstall / Restore). Native <summary> is
// keyboard-operable for free; the .expand-icon chevron rotates via the
// details[open] CSS rule (style-guide §9.13) — no Unicode triangle, no custom
// toggle handler. Hidden entirely when the list is empty.
function _renderRoutedOffAffordance(boxId, items, bot, summaryHtml, introHtml) {
  const box = document.getElementById(boxId);
  if (!box) return;
  if (!items.length) { box.style.display = 'none'; box.innerHTML = ''; return; }
  box.style.display = '';
  const rows = items.map(c => {
    const objective = c.description || '';
    return `<div style="display:flex;align-items:flex-start;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)">
      <div style="flex:1;min-width:0">
        <div style="font-size:0.82rem;color:var(--text)">${escHtml(c.name || c.id || '?')}</div>
        ${objective ? `<div style="font-size:0.72rem;color:var(--text2);margin-top:2px">${escHtml(objective)}</div>` : ''}
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:5px;flex-shrink:0">${_capActionBtns(c, bot)}</div>
    </div>`;
  }).join('');
  const intro = introHtml
    ? `<div style="font-size:0.72rem;color:var(--text2);margin:6px 0 2px">${introHtml}</div>`
    : '';
  box.innerHTML = `<details style="background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:10px 14px">
    <summary style="display:flex;align-items:center;gap:8px;font-size:0.82rem;color:var(--text2)">
      <span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
      ${summaryHtml}
    </summary>
    <div style="margin-top:8px;border-top:1px solid var(--border);padding-top:4px">${intro}${rows}</div>
  </details>`;
}

// Capabilities (reusable skills, app_kind="capability"). Routed off the grid in
// renderCapabilities(); still real installed code, so full lifecycle controls.
function _renderCapabilitiesAffordance(capabilities, bot) {
  const n = capabilities.length;
  const noun = n === 1 ? 'capability (not an application)' : 'capabilities (not applications)';
  const summary = `<span style="color:var(--text)">${n} ${noun}</span>`
    + `<span style="color:var(--text3)">— reusable skills, kept off the apps grid</span>`;
  _renderRoutedOffAffordance('cap-capabilities-affordance', capabilities, bot, summary);
}

// Pod system functions (agent-runtime infrastructure, app_kind="system", the
// classifier's "system" verdict — PR #3060). System ≠ capability: a capability
// is a reusable skill, system is the agent's own runtime plumbing. Separate
// labeled affordance, parallel to capabilities, with the same lifecycle controls.
function _renderSystemAffordance(systemFns, bot) {
  const n = systemFns.length;
  const noun = n === 1 ? 'pod system function (not an application)' : 'pod system functions (not applications)';
  const summary = `<span style="color:var(--text)">${n} ${noun}</span>`
    + `<span style="color:var(--text3)">— kept running, not a user app</span>`;
  const intro = 'Pod / agent-runtime infrastructure the scanner classified as '
    + '<code>system</code> — self-healing, heartbeat/liveness, scheduling, and the '
    + "pod's own cost/warning telemetry. Kept running, but not user-facing applications.";
  _renderRoutedOffAffordance('cap-system-affordance', systemFns, bot, summary, intro);
}

// ── Manifest Modal ────────────────────────────────────────────────────────────
// State for the currently open manifest
let _mBotId = null, _mAppId = null, _mData = null, _mEditMode = false, _mStem = null;

async function viewManifest(botId, appId, stem) {
  const m = await api('GET', `/api/applications/${botId}/${appId}`);
  if (m.error) { toast('✗ ' + m.error, 'err'); return; }
  _mBotId = botId; _mAppId = appId; _mData = m; _mEditMode = false;
  // The filename stem the definition endpoints write back to (§9.7 finding
  // #5). When opened from a tile we pass it; other entry points (coherence /
  // adopt) omit it and the endpoint's own stem-resolution covers the gap.
  _mStem = stem || appId;
  _renderManifestModal();
  document.getElementById('manifest-modal').classList.add('open');
}

function _renderManifestModal() {
  const esc = escHtml;
  const m = _mData;
  const edit = _mEditMode;
  const identity = m.identity || {};
  const sc = m.success_criteria || {};
  const co = m.constraints || {};
  const sat = m.satisfaction || {};
  const satScore = sat.score ?? m.satisfaction_score ?? null;
  const conf = m.confidence != null ? Math.round(m.confidence * 100) + '%' : 'n/a';

  const secStyle = 'border:1px solid var(--border);border-radius:8px;padding:14px 16px;margin-bottom:12px;background:var(--bg2)';
  const secHead = (title, why) => `<div style="font-size:0.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);margin-bottom:2px">${title}</div><div style="font-size:0.72rem;color:var(--text2);margin-bottom:10px;font-style:italic">${why}</div>`;

  // Helper: render a list (read) or textarea (edit)
  const listView = (items) => items && items.length
    ? `<ul style="margin:4px 0 0 18px;padding:0">${items.map(i=>`<li style="margin-bottom:3px">${esc(i)}</li>`).join('')}</ul>`
    : '<div style="color:var(--text3);font-size:0.8rem">—</div>';

  const listEdit = (id, items) => {
    const val = (items || []).join('\n');
    return `<textarea id="${id}" rows="${Math.max(3, (items||[]).length + 1)}" style="width:100%;font-size:0.8rem;font-family:inherit;background:var(--bg1);border:1px solid var(--border);border-radius:5px;padding:6px 8px;color:var(--text1);resize:vertical">${esc(val)}</textarea><div style="font-size:0.68rem;color:var(--text3);margin-top:2px">One item per line</div>`;
  };

  const field = (label, viewHtml, editHtml) => `
    <div style="margin-bottom:10px">
      <div style="font-size:0.73rem;font-weight:600;color:var(--text2);margin-bottom:4px">${label}</div>
      ${edit ? editHtml : viewHtml}
    </div>`;

  const textView = (val) => val ? `<div style="color:var(--text1)">${esc(val)}</div>` : '<div style="color:var(--text3)">—</div>';
  const textEdit = (id, val, ph='') => `<input id="${id}" value="${esc(val||'')}" placeholder="${ph}" style="width:100%;font-size:0.82rem;font-family:inherit;background:var(--bg1);border:1px solid var(--border);border-radius:5px;padding:6px 8px;color:var(--text1)">`;

  // ── Section 1: IDENTITY ────────────────────────────────────────────────────
  const identitySection = `<div style="${secStyle}">
    ${secHead('Identity', 'What is this application and why does it exist?')}
    ${field('Purpose', textView(identity.purpose), textEdit('m-purpose', identity.purpose, 'This application exists to…'))}
    ${field('Scope — includes', listView(identity.scope_includes), listEdit('m-scope-in', identity.scope_includes))}
    ${field('Scope — excludes', listView(identity.scope_excludes), listEdit('m-scope-out', identity.scope_excludes))}
    ${field('User', textView(identity.user), textEdit('m-user', identity.user, 'who this is for'))}
  </div>`;

  // ── Section 2: SUCCESS CRITERIA ────────────────────────────────────────────
  const qb = sc.quality_bar || {};
  const successSection = `<div style="${secStyle}">
    ${secHead('Success Criteria', 'How will the AI know if this is working? These criteria drive automated QA.')}
    ${field('Observable outcomes', listView(sc.observable_outcomes), listEdit('m-outcomes', sc.observable_outcomes))}
    ${field('Failure signals', listView(sc.failure_signals), listEdit('m-failure', sc.failure_signals))}
    ${field('Minimum bar', textView(qb.minimum), textEdit('m-qb-min', qb.minimum, 'Minimum acceptable performance'))}
    ${field('Excellent bar', textView(qb.excellent), textEdit('m-qb-exc', qb.excellent, 'Excellent performance looks like…'))}
  </div>`;

  // ── Section 3: CONSTRAINTS ─────────────────────────────────────────────────
  const constraintsSection = `<div style="${secStyle}">
    ${secHead('Constraints', 'What must always be true? These protect privacy and define boundaries.')}
    ${field('Privacy', listView(co.privacy), listEdit('m-privacy', co.privacy))}
    ${field('Safety', listView(co.safety), listEdit('m-safety', co.safety))}
    ${field('Dependencies', listView(co.dependencies), listEdit('m-deps', co.dependencies))}
    ${field('Boundaries', listView(co.boundaries), listEdit('m-bounds', co.boundaries))}
  </div>`;

  // Test-cases / cadence / exemption sections removed 2026-06-08.
  // The app-test surface was killed per docs/decision-app-tests-2026-06-08.md.
  // Manifest fields retained on-disk for backward compatibility; UI no
  // longer exposes editors.
  const testSection = '';
  const testingConfigSection = '';

  // ── Audit Configuration + Run / Trail (Tier 3) ──────────────────────────────
  const auditCadence = m.audit_cadence;
  const auditEligible = m.audit_eligible !== false;
  const lastAudit = m.last_audit || {};
  const accepted = m.audit_accepted || [];
  const auditCadenceOption = (val, label) =>
    `<option value="${val}" ${auditCadence === (val || null) ? 'selected' : ''}>${label}</option>`;
  const lastAuditBadge = (() => {
    if (!auditEligible) return '<span style="color:var(--text3)">⊘ Ineligible for auto-audit (manual only)</span>';
    if (!lastAudit || !lastAudit.verified_at) return '<span style="color:var(--text3)">– Never audited</span>';
    if (lastAudit.status === 'failed') {
      const err = esc((lastAudit.error || '').slice(0, 80));
      return `<span style="color:var(--red)">❌ Last audit failed: ${err}</span>`;
    }
    const outcomes = lastAudit.outcomes || {};
    const raised = (outcomes.propose || 0) + (outcomes.conflict_notice || 0);
    const verifiedAt = esc(lastAudit.verified_at);
    if (raised > 0) {
      return `<span style="color:var(--yellow)">⚠ ${raised} raised · ${lastAudit.findings_count || 0} total</span> <span style="color:var(--text3);font-size:0.75rem">· ${verifiedAt}</span>`;
    }
    return `<span style="color:var(--green)">✓ Clean</span> <span style="color:var(--text3);font-size:0.75rem">· ${verifiedAt}</span>`;
  })();
  const auditButtonsHtml = `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">
    <button class="btn-soft" onclick="_runAuditNow(false)" style="font-size:0.78rem">Run audit now</button>
    <button class="btn-soft" onclick="_runAuditNow(true)" style="font-size:0.78rem" title="Re-evaluate accepted findings too">Full audit</button>
    <button class="btn-soft" onclick="_viewAuditTrail()" style="font-size:0.78rem">View trail</button>
  </div>`;
  const acceptedListHtml = accepted.length > 0
    ? `<div style="margin-top:10px">
        <div style="font-size:0.73rem;font-weight:600;color:var(--text2);margin-bottom:4px">Accepted findings (${accepted.length})</div>
        <ul style="margin:0;padding-left:18px;font-size:0.78rem">${accepted.map(a => {
          const sig = esc(((a && a.signature) || '').slice(0, 16) + '…');
          const why = esc((a && a.rationale) || '');
          return `<li style="margin-bottom:3px">
            <code>${sig}</code> ${why ? `— ${why}` : ''}
            <a href="#" onclick="_unacceptFinding('${esc((a && a.signature) || '')}'); return false" style="margin-left:8px;color:var(--text3);font-size:0.72rem">un-accept</a>
          </li>`;
        }).join('')}</ul>
      </div>`
    : '';
  const auditConfigSection = `<div style="${secStyle}">
    ${secHead('App Audit (Tier 3)', 'Semantic audit — does the code still do what the manifest claims? See docs/spec-app-audit-2026-05-16.md.')}
    ${field('Last audit', `<div>${lastAuditBadge}</div>`, '')}
    ${field('Cadence',
      textView(auditCadence == null ? 'Inherit pod / bot default' : auditCadence),
      `<select id="m-audit-cadence" style="width:100%;font-size:0.82rem;font-family:inherit;background:var(--bg1);border:1px solid var(--border);border-radius:5px;padding:6px 8px;color:var(--text1)">
        ${auditCadenceOption('', 'Inherit pod / bot default')}
        ${auditCadenceOption('never', 'never — no auto audit (manual only)')}
        ${auditCadenceOption('quarterly', 'quarterly — every 90 days')}
        ${auditCadenceOption('monthly', 'monthly — every 30 days')}
        ${auditCadenceOption('weekly', 'weekly — every 7 days')}
        ${auditCadenceOption('daily', 'daily — every 24 hours (cost-flag)')}
      </select>`)}
    ${field('Auto-audit eligible',
      `<div>${auditEligible ? '<span style="color:var(--green)">Yes</span>' : '<span style="color:var(--text3)">No — manual audits only</span>'}</div>`,
      `<label style="font-size:0.82rem"><input type="checkbox" id="m-audit-eligible" ${auditEligible ? 'checked' : ''}> Auto-audit eligible</label>`)}
    ${auditButtonsHtml}
    ${acceptedListHtml}
  </div>`;

  // ── Example triggers ───────────────────────────────────────────────────────
  const triggers = m.example_triggers || [];
  const triggersSection = triggers.length > 0 || edit ? `<div style="${secStyle}">
    ${secHead('Example Triggers', 'User messages that invoke this application.')}
    ${field('Triggers', listView(triggers), listEdit('m-triggers', triggers))}
  </div>` : '';

  // ── Section 5: SATISFACTION ────────────────────────────────────────────────
  const starHtml = (score) => [1,2,3,4,5].map(n =>
    `<span class="star" style="font-size:1.4rem;cursor:pointer;color:${n<=(score||0)?'var(--yellow)':'var(--text3)'}" onclick="_setStar(${n})" title="${n} star${n>1?'s':''}">${n<=(score||0)?'★':'☆'}</span>`
  ).join('');

  const satNotes = sat.notes ?? m.satisfaction_notes ?? '';
  const satSection = `<div style="${secStyle}">
    ${secHead('Satisfaction', 'Subjective quality rating. Used to track improvement over time.')}
    <div style="margin-bottom:8px">
      <div style="font-size:0.73rem;font-weight:600;color:var(--text2);margin-bottom:4px">Rating</div>
      <div id="m-stars">${starHtml(satScore)}</div>
      <input type="hidden" id="m-sat-score" value="${satScore ?? ''}">
    </div>
    <div>
      <div style="font-size:0.73rem;font-weight:600;color:var(--text2);margin-bottom:4px">Notes</div>
      ${edit
        ? `<textarea id="m-sat-notes" rows="2" style="width:100%;font-size:0.8rem;font-family:inherit;background:var(--bg1);border:1px solid var(--border);border-radius:5px;padding:6px 8px;color:var(--text1);resize:vertical">${esc(satNotes)}</textarea>`
        : (satNotes ? `<div style="color:var(--text1)">${esc(satNotes)}</div>` : '<div style="color:var(--text3)">—</div>')}
    </div>
  </div>`;

  // ── Improvement history ────────────────────────────────────────────────────
  const history = m.improvement_history || [];
  const histSection = history.length > 0 ? `<div style="${secStyle}">
    ${secHead('Improvement History', 'What has been tried and learned. Prevents repeating failed experiments.')}
    <ul style="margin:0 0 0 18px;padding:0">${history.map(h =>
      `<li style="margin-bottom:4px;font-size:0.8rem">${esc(typeof h === 'string' ? h : JSON.stringify(h))}</li>`
    ).join('')}</ul>
  </div>` : '';

  // ── Known issues ───────────────────────────────────────────────────────────
  const issues = m.known_issues || [];
  const issuesSection = issues.length > 0 || edit ? `<div style="${secStyle}">
    ${secHead('Known Issues', 'Open problems or degraded behavior.')}
    ${field('Issues', listView(issues), listEdit('m-issues', issues))}
  </div>` : '';

  // ── Lifecycle section: REMOVED 2026-05-26 ──────────────────────────────────
  // The Spec Drafted → Spec Approved → Prototype Built → Tests Run → QA → RSI
  // → Complete progression is forge-job state, not installed-app state. The
  // Forge Jobs tab (renderForgeJobs) owns the real build lifecycle for
  // in-flight apps; installed-app cards show operational state only.
  //
  // 2026-05-27 follow-on: also stripped the _lcAction / _lcViewSpec JS
  // handlers (used to live below) and removed the `lifecycle: {...}` init
  // from the user_created POST handler in server.py — manifests no longer
  // carry a `lifecycle` field at all. Existing on-disk manifests with
  // orphan lifecycle blobs (written by old "Draft Spec" clicks) are
  // harmless and left alone; nothing reads them.
  //
  // The "how was this app created" question — answered today by the
  // existing `source` field (bot_created / user_created / discovered /
  // gallery_installed / file_imported / rsi_proposed). See the header
  // section below for the friendly label + tooltip that surfaces it.

  // ── Header ─────────────────────────────────────────────────────────────────
  // `source` records how the app got into this bot's workspace. Used to be
  // shown as just a single-word badge ("discovered" / "user_created" / etc.)
  // which the operator had to decode mentally — the 2026-05-27 cleanup
  // replaced that with friendly labels ("via scanner", "via forge", ...) and
  // a tooltip that spells out what the value means. The badge class still
  // colour-codes it for fast visual scan.
  const _srcInfo = {
    bot_created:      { label: 'via forge',    cls: 'forge',      tip: 'Built by the forge chat wizard from a natural-language description.' },
    user_created:     { label: 'via wizard',   cls: 'ok',         tip: 'Drafted in the admin Create tab and saved as a direct manifest write.' },
    discovered:       { label: 'via scanner',  cls: 'detected',   tip: 'Inferred from workspace files by the application scanner — not a build artefact.' },
    gallery_installed:{ label: 'from gallery', cls: 'app',        tip: 'Installed from the application gallery (a published Spec).' },
    file_imported:    { label: 'imported',     cls: 'member',     tip: 'Imported from an external manifest file.' },
    rsi_proposed:     { label: 'via RSI',      cls: 'autonomous', tip: 'Proposed by an RSI generator and accepted into this bot.' },
  };
  const srcKey = m.source || 'discovered';
  const srcInfo = _srcInfo[srcKey] || { label: srcKey.replace(/_/g, ' '), cls: 'detected', tip: `source: ${srcKey}` };
  const sourceBadgeCls = srcInfo.cls;
  const srcLabel = srcInfo.label;
  const srcTooltip = `${srcInfo.tip} (source: ${srcKey})`;

  // Only show priority badge when it carries information (not the default "feature")
  const priorityBadge = m.priority && m.priority !== 'feature'
    ? badge(m.priority, m.priority === 'core' ? 'crit' : 'member') : '';

  // pkg_id chip (monospace, subtle)
  const pkgIdChip = m.pkg_id
    ? `<code style="font-size:0.7rem;background:var(--bg3);color:var(--text3);padding:1px 6px;border-radius:4px;font-family:monospace;user-select:all" title="Stable package ID">${esc(m.pkg_id)}</code>`
    : '';

  // Provenance date line
  const _fmtDate = iso => iso ? iso.slice(0, 10) : null;
  const createdFmt = _fmtDate(m.created_at);
  const updatedFmt = _fmtDate(m.updated_at);
  const dateParts = [];
  if (createdFmt) dateParts.push(`created ${createdFmt}`);
  if (updatedFmt && updatedFmt !== createdFmt) dateParts.push(`updated ${updatedFmt}`);
  if (m.source_detail) dateParts.push(m.source_detail);
  const dateLine = dateParts.length
    ? `<div style="font-size:0.7rem;color:var(--text3);margin-top:4px">${dateParts.map(esc).join(' · ')}</div>`
    : '';

  const header = `<div style="display:flex;align-items:flex-start;gap:10px;margin-bottom:16px">
    <div style="flex:1">
      <div style="font-size:1.1rem;font-weight:700;margin-bottom:6px">${esc(m.name || _mAppId)}</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
        <span title="${esc(srcTooltip)}" style="cursor:help">${badge(srcLabel, sourceBadgeCls)}</span>
        ${m.confidence != null ? badge(`conf: ${conf}`, 'ok') : ''}
        ${badge(m.status || 'draft', m.status === 'active' || m.status === 'approved' ? 'ok' : 'warn')}
        ${priorityBadge}
        ${badge(`schema v${m.schema_version || 1}`, 'member')}
        ${pkgIdChip}
      </div>
      ${dateLine}
    </div>
    <div style="display:flex;gap:6px;flex-shrink:0">
      ${edit
        ? `<button class="btn btn-primary btn-sm" onclick="saveManifest()">Save</button>
           <button class="btn btn-ghost btn-sm" onclick="_cancelEdit()">Cancel</button>`
        : `<button class="btn btn-ghost btn-sm" onclick="editManifest()">Edit</button>
           <button class="btn btn-danger btn-sm" onclick="_confirmDeleteApp('${escHtml(_mBotId)}','${escHtml(_mAppId)}',${escHtml(JSON.stringify(m.name||_mAppId))})" title="Remove this app and clean up its files">🗑 Delete</button>
           <button class="btn btn-ghost btn-sm" onclick="closeManifestModal()">✕</button>`}
    </div>
  </div>`;

  // ── Files & Resources section ───────────────────────────────────────────────
  const _layerBadgeCls = {
    script: 'forge', skill: 'agent', policy: 'security_bot', orchestrator: 'autonomous',
    test: 'inline', reference: 'member', data: 'ok', state: 'warn'
  };
  const _lcDotColor = {
    owned: '#4ade80', shared: '#7eb8f7',
    // "external" replaces the old "orphaned" label (production
    // calibration 2026-06-08: personal-bot's Biometric Integration showed
    // its own files as "orphaned" because their owned_by pointed at a
    // different pkg_id — confusing, since orphan implies "dead").
    // Amber dot, not red — it's a different-ownership signal, not
    // a critical error.
    external: '#f59e0b',
    // Keep the old key around for backward compat if the index
    // returns "orphaned" verbatim during the migration window.
    orphaned: '#f59e0b',
    unowned: 'var(--text3)'
  };

  const rawFiles = Array.isArray(m.files) ? m.files : [];
  const fileRows = rawFiles.map(f => {
    const isStr = typeof f === 'string';
    const fpath    = isStr ? f : (f.path || '');
    const fileId   = isStr ? '' : (f.file_id || '');
    const layer    = isStr ? '' : (f.layer || '');
    const purpose  = isStr ? '' : (f.purpose || '');
    const ownedBy  = isStr ? '' : (f.owned_by || '');
    const sharedWith = isStr ? [] : (f.shared_with || []);
    // Derive lifecycle from ownership metadata
    let lc2 = 'unowned';
    if (ownedBy === m.pkg_id) lc2 = sharedWith.length > 0 ? 'shared' : 'owned';
    else if (ownedBy) lc2 = 'external'; // owned by a different app (pkg_id ownedBy)
    else if (fileId) lc2 = 'owned';
    // Backward compat: index may still return "orphaned" verbatim;
    // normalize to "external" for display while we migrate.
    if (lc2 === 'orphaned') lc2 = 'external';
    const dotColor = _lcDotColor[lc2] || 'var(--text3)';
    const fileName = fpath.split('/').pop() || fpath;
    // Tooltip when external — explains that the file belongs to a
    // different app's manifest, not that it's broken.
    const lcTitle = lc2 === 'external'
      ? `Owned by another app's manifest (pkg_id ${ownedBy}). Not orphaned — just attributed elsewhere.`
      : '';
    return `<tr style="border-bottom:1px solid var(--border)">
      <td style="padding:5px 8px 5px 0;vertical-align:top">
        <div style="font-family:monospace;font-size:0.72rem;color:var(--text1)">${esc(fileName)}</div>
        <div style="font-size:0.65rem;color:var(--text3);word-break:break-all">${esc(fpath)}</div>
      </td>
      <td style="padding:5px 8px;vertical-align:top;white-space:nowrap">${layer ? badge(layer, _layerBadgeCls[layer] || 'member') : '<span style="color:var(--text3)">—</span>'}</td>
      <td style="padding:5px 8px;vertical-align:top;white-space:nowrap"${lcTitle ? ` title="${esc(lcTitle)}"` : ''}>
        <span style="display:inline-flex;align-items:center;gap:4px">
          <span style="width:7px;height:7px;border-radius:50%;background:${dotColor};flex-shrink:0"></span>
          <span style="font-size:0.72rem;color:var(--text2)">${lc2}</span>
        </span>
      </td>
      <td style="padding:5px 8px;vertical-align:top;font-family:monospace;font-size:0.7rem;color:var(--text3);white-space:nowrap">${esc(fileId || '—')}</td>
      <td style="padding:5px 0 5px 8px;vertical-align:top;font-size:0.72rem;color:var(--text2)">${esc(purpose || '—')}</td>
    </tr>`;
  }).join('');

  const rawCrons = Array.isArray(m.crons) ? m.crons : [];
  const cronRows = rawCrons.map(c => {
    const isStr = typeof c === 'string';
    const schedule = isStr ? c : (c.schedule || c.cron || '');
    const script   = isStr ? '' : (c.script || '');
    const label    = isStr ? '' : (c.label || c.description || '');
    const cronFileId = isStr ? '' : (c.file_id || '');
    return `<tr style="border-bottom:1px solid var(--border)">
      <td style="padding:5px 8px 5px 0;font-family:monospace;font-size:0.72rem;color:var(--text1);white-space:nowrap">${esc(schedule)}</td>
      <td style="padding:5px 8px;font-family:monospace;font-size:0.72rem;color:var(--text2)">${esc(script || '—')}</td>
      <td style="padding:5px 8px;font-size:0.72rem;color:var(--text2)">${esc(label || '—')}</td>
      <td style="padding:5px 0;font-family:monospace;font-size:0.7rem;color:var(--text3)">${esc(cronFileId || '—')}</td>
    </tr>`;
  }).join('');

  // ── Producers (v23 admin-ui) ─────────────────────────────────────────────
  // Derive trigger-surface rows from scheduled_actions[], heartbeat_evidence,
  // and cron_evidence.labels[]. Files[] tells you what an app OWNS; this tells
  // you what DRIVES it. Pre-v23 the operator had to read the manifest JSON to
  // see that HEARTBEAT.md / AGENTS.md / a launchd label was the producer —
  // Files & Resources was where they looked first, and it was silent on this.
  //
  // Pure derivation: every row is computed from already-present manifest
  // fields. Safe to render even on manifests that pre-date Layer B's
  // cron_evidence population (the rows just come from scheduled_actions
  // alone in that case).
  const rawSchedActions = Array.isArray(m.scheduled_actions) ? m.scheduled_actions : [];
  const heartbeatEvidence = (m.heartbeat_evidence && typeof m.heartbeat_evidence === 'object') ? m.heartbeat_evidence : {};
  const cronEvidence = (m.cron_evidence && typeof m.cron_evidence === 'object') ? m.cron_evidence : {};
  const producerRows = [];
  const _seenProducers = new Set();
  // Anchor representations differ across the two producer sources:
  //   - scheduled_actions[].install.section_anchor stores the canonical
  //     heading form "## Memory Files" (scanner._build_scheduled_action_
  //     from_instruction prepends "## ").
  //   - heartbeat_evidence.section_anchors stores the raw anchor "Memory
  //     Files" (no leading hashes).
  // Dedup must use a normalized key or the same anchor surfaces twice
  // for any HEARTBEAT.md app whose evidence is mirrored in both fields.
  const _normAnchor = (a) => String(a || '').replace(/^#+\s*/, '').trim();
  const _pushProducer = (source, anchor, kind, schedule) => {
    if (!source) return;
    const key = `${kind}::${source}::${_normAnchor(anchor)}`;
    if (_seenProducers.has(key)) return;
    _seenProducers.add(key);
    producerRows.push({ source, anchor, kind, schedule });
  };
  for (const sa of rawSchedActions) {
    if (!sa || typeof sa !== 'object') continue;
    const install = (sa.install && typeof sa.install === 'object') ? sa.install : {};
    const trigger = (sa.trigger && typeof sa.trigger === 'object') ? sa.trigger : {};
    const kind = trigger.kind || sa.mechanism || '';
    const schedule = trigger.schedule || '';
    const file = install.file || '';
    const anchor = install.section_anchor || '';
    const plistLabel = install.plist_label || '';
    if (file) {
      _pushProducer(file, anchor, kind, schedule);
    } else if (plistLabel) {
      _pushProducer(plistLabel, '', kind || 'launchd', schedule);
    } else if (install.command) {
      // Last-resort fallback: a scheduled action with neither file nor
      // plist_label but a command. Show the command so the row isn't empty.
      _pushProducer(install.command, '', kind, schedule);
    }
  }
  // heartbeat_evidence: pod-wide HEARTBEAT.md anchors (v13 contract).
  if (heartbeatEvidence.file_path && Array.isArray(heartbeatEvidence.section_anchors)) {
    for (const a of heartbeatEvidence.section_anchors) {
      _pushProducer(heartbeatEvidence.file_path, a || '', 'heartbeat', 'every_heartbeat');
    }
  }
  // cron_evidence.labels: Layer B-populated launchd labels.
  const ceLabels = Array.isArray(cronEvidence.labels) ? cronEvidence.labels : [];
  for (const label of ceLabels) {
    _pushProducer(label, '', 'launchd', '');
  }

  const hasFiles = rawFiles.length > 0;
  const hasCrons = rawCrons.length > 0;
  const hasProducers = producerRows.length > 0;

  // _buildFilesTable: shared renderer used by initial render + async index update
  // indexMap: {file_id → index record} or null (no index yet)
  // unregistered: [{file_id, path, layer, lifecycle, ...}] extra from index
  const _buildFilesSection = (indexMap, unregistered) => {
    const th = txt => `<th style="text-align:left;padding:3px 8px 6px 0;font-size:0.67rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3)">${txt}</th>`;
    const thm = txt => `<th style="text-align:left;padding:3px 8px 6px;font-size:0.67rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3)">${txt}</th>`;

    const fRows = rawFiles.map(f => {
      const isStr = typeof f === 'string';
      const fpath    = isStr ? f : (f.path || '');
      const fileId   = isStr ? '' : (f.file_id || '');
      const layer    = isStr ? '' : ((indexMap && fileId && indexMap[fileId] ? indexMap[fileId].layer : '') || f.layer || '');
      const purpose  = isStr ? '' : (f.purpose || '');
      const ownedBy  = isStr ? '' : (f.owned_by || '');
      const sharedWith = isStr ? [] : (f.shared_with || []);
      // Use authoritative lifecycle from index if available, else derive from manifest
      let lc2 = (indexMap && fileId && indexMap[fileId]) ? indexMap[fileId].lifecycle : 'unowned';
      if (!indexMap || !(fileId && indexMap[fileId])) {
        if (ownedBy === m.pkg_id) lc2 = sharedWith.length > 0 ? 'shared' : 'owned';
        else if (ownedBy) lc2 = 'external'; // owned by a different app
        else if (fileId) lc2 = 'owned';
      }
      // Backward compat: normalize legacy "orphaned" → "external" for display.
      if (lc2 === 'orphaned') lc2 = 'external';
      const dotColor = _lcDotColor[lc2] || 'var(--text3)';
      const fileName = fpath.split('/').pop() || fpath;
      const lcTitle = lc2 === 'external'
        ? `Owned by another app's manifest (pkg_id ${ownedBy || '?'}). Not orphaned — just attributed elsewhere.`
        : '';
      return `<tr style="border-bottom:1px solid var(--border)">
        <td style="padding:5px 8px 5px 0;vertical-align:top">
          <div style="font-family:monospace;font-size:0.72rem;color:var(--text1)">${escHtml(fileName)}</div>
          <div style="font-size:0.65rem;color:var(--text3);word-break:break-all">${escHtml(fpath)}</div>
        </td>
        <td style="padding:5px 8px;vertical-align:top;white-space:nowrap">${layer ? badge(layer, _layerBadgeCls[layer] || 'member') : '<span style="color:var(--text3)">—</span>'}</td>
        <td style="padding:5px 8px;vertical-align:top;white-space:nowrap"${lcTitle ? ` title="${escHtml(lcTitle)}"` : ''}>
          <span style="display:inline-flex;align-items:center;gap:4px">
            <span style="width:7px;height:7px;border-radius:50%;background:${dotColor};flex-shrink:0"></span>
            <span style="font-size:0.72rem;color:var(--text2)">${lc2}</span>
          </span>
        </td>
        <td style="padding:5px 8px;vertical-align:top;font-family:monospace;font-size:0.7rem;color:var(--text3);white-space:nowrap">${escHtml(fileId || '—')}</td>
        <td style="padding:5px 0 5px 8px;vertical-align:top;font-size:0.72rem;color:var(--text2)">${escHtml(purpose || '—')}</td>
      </tr>`;
    }).join('');

    const unregRows = (unregistered || []).map(r => {
      const fileName = (r.path || '').split('/').pop() || r.path;
      const dotColor = _lcDotColor[r.lifecycle] || 'var(--text3)';
      return `<tr style="border-bottom:1px solid var(--border)">
        <td style="padding:5px 8px 5px 0;vertical-align:top">
          <div style="font-family:monospace;font-size:0.72rem;color:var(--text1)">${escHtml(fileName)}</div>
          <div style="font-size:0.65rem;color:var(--text3);word-break:break-all">${escHtml(r.path || '')}</div>
        </td>
        <td style="padding:5px 8px;vertical-align:top;white-space:nowrap">${r.layer ? badge(r.layer, _layerBadgeCls[r.layer] || 'member') : '<span style="color:var(--text3)">—</span>'}</td>
        <td style="padding:5px 8px;vertical-align:top;white-space:nowrap">
          <span style="display:inline-flex;align-items:center;gap:4px">
            <span style="width:7px;height:7px;border-radius:50%;background:${dotColor};flex-shrink:0"></span>
            <span style="font-size:0.72rem;color:var(--text2)">${escHtml(r.lifecycle || 'unowned')}</span>
          </span>
        </td>
        <td style="padding:5px 8px;vertical-align:top;font-family:monospace;font-size:0.7rem;color:var(--text3);white-space:nowrap">${escHtml(r.file_id || '—')}</td>
        <td style="padding:5px 0 5px 8px;vertical-align:top;font-size:0.72rem;color:var(--yellow)">not in manifest</td>
      </tr>`;
    }).join('');

    // Only show spinner if we have a pkg_id and will actually fire the async fetch
    const indexNote = (indexMap === null && m.pkg_id)
      ? `<div style="font-size:0.68rem;color:var(--text3);margin-top:8px">⏳ Loading index…</div>`
      : '';

    const hasUnreg = unregRows.length > 0;
    return `
    ${hasFiles || hasUnreg ? `<div style="overflow-x:auto${hasCrons ? ';margin-bottom:16px' : ''}">
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="border-bottom:1px solid var(--border)">
          ${th('Path')}${thm('Layer')}${thm('Lifecycle')}${thm('File ID')}<th style="text-align:left;padding:3px 0 6px 8px;font-size:0.67rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3)">Purpose</th>
        </tr></thead>
        <tbody>${fRows}${unregRows}</tbody>
      </table>
    </div>` : `<div style="font-size:0.78rem;color:var(--text3);margin-bottom:${hasCrons ? '14px' : '0'}">No files registered. Run a workspace scan to discover and link component files.</div>`}
    ${indexNote}
    ${hasCrons ? `<div style="font-size:0.72rem;font-weight:700;color:var(--text2);text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px${hasFiles || hasUnreg ? ';margin-top:4px' : ''}">Cron jobs</div>
    <div style="overflow-x:auto${hasProducers ? ';margin-bottom:16px' : ''}">
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="border-bottom:1px solid var(--border)">
          ${th('Schedule')}${thm('Script')}${thm('Label')}<th style="text-align:left;padding:3px 0 6px 8px;font-size:0.67rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3)">File ID</th>
        </tr></thead>
        <tbody>${cronRows}</tbody>
      </table>
    </div>` : ''}
    ${hasProducers ? `<div style="font-size:0.72rem;font-weight:700;color:var(--text2);text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px${hasFiles || hasUnreg || hasCrons ? ';margin-top:4px' : ''}" title="Files and labels that drive this app — HEARTBEAT.md / AGENTS.md sections, launchd plists, etc. Distinct from owned files above.">Producers</div>
    <div style="overflow-x:auto">
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="border-bottom:1px solid var(--border)">
          ${th('Source')}${thm('Anchor')}${thm('Kind')}<th style="text-align:left;padding:3px 0 6px 8px;font-size:0.67rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3)">Schedule</th>
        </tr></thead>
        <tbody>${producerRows.map(p => {
          const sourceFile = (p.source || '').split('/').pop() || p.source;
          return `<tr style="border-bottom:1px solid var(--border)">
            <td style="padding:5px 8px 5px 0;vertical-align:top">
              <div style="font-family:monospace;font-size:0.72rem;color:var(--text1)">${escHtml(sourceFile)}</div>
              ${sourceFile !== p.source ? `<div style="font-size:0.65rem;color:var(--text3);word-break:break-all">${escHtml(p.source)}</div>` : ''}
            </td>
            <td style="padding:5px 8px;vertical-align:top;font-family:monospace;font-size:0.7rem;color:var(--text2)">${escHtml(p.anchor || '—')}</td>
            <td style="padding:5px 8px;vertical-align:top;white-space:nowrap">${p.kind ? badge(p.kind, _layerBadgeCls[p.kind] || 'member') : '<span style="color:var(--text3)">—</span>'}</td>
            <td style="padding:5px 0 5px 8px;vertical-align:top;font-family:monospace;font-size:0.7rem;color:var(--text2)">${escHtml(p.schedule || '—')}</td>
          </tr>`;
        }).join('')}</tbody>
      </table>
    </div>` : ''}`;
  };

  const filesSection = `<div id="manifest-files-section" style="${secStyle}">
    ${secHead('Files & Resources', 'Component files and cron jobs the app owns, plus the Producers that drive it — HEARTBEAT.md / AGENTS.md sections, launchd plists, etc. File IDs are stable across renames; layer determines removal policy.')}
    <div id="manifest-files-body">${_buildFilesSection(null, [])}</div>
  </div>`;

  // ── Section: LESSONS (v7-arc only) ──────────────────────────────────────────
  // For v7-arc Instances, surface a "Lessons" section that fetches asynchronously
  // and shows count + redaction status + Share button. Hidden for v13 legacy
  // (no provenance.spec_id) and for v7-arc Instances with no Lessons file
  // (the fetch returns 404; the section just disappears).
  const v7SpecId = (m.provenance || {}).spec_id;
  const lessonsSection = v7SpecId ? `<div id="manifest-lessons-section" style="${secStyle}">
    ${secHead('Lessons', "Evidence-cited distillation of this app's change log + usage. Redacted before share. Compress via the lessons_compress CLI.")}
    <div id="manifest-lessons-body">
      <div style="font-size:0.78rem;color:var(--text3)">Loading…</div>
    </div>
  </div>` : '';

  // Spec-version-delta section (v7-arc only) — lazy-fetched. Hidden by
  // default; the fetch populates the body if drift exists, otherwise
  // removes the section entirely. Shows current → latest with the per-bucket
  // field diff + a "Review & adopt" button (deeplinks to the Adopt modal).
  const versionDeltaSection = v7SpecId ? `<div id="manifest-version-delta-section" style="${secStyle};display:none">
    ${secHead('Spec version', 'Currently bound Spec version vs latest in the gallery. When drift exists you can review the diff here or jump to the Adopt review.')}
    <div id="manifest-version-delta-body"></div>
  </div>` : '';

  // ── Coherence + Drift section (spec §10–§11) ──────────────────────────────
  // Built last so it has access to the latest manifest dict. Sits adjacent
  // to Audit since both are "is this app healthy?" surfaces.
  const coherenceSection = _renderCoherenceSection(m, secStyle, secHead, esc);

  // ── Definition (Defined/Discovered) section (spec §9, Bite 4) ─────────────
  // The operator's source-of-truth vouch, the unreviewed-drift review entry,
  // and a one-way cross-link to "Make reliable" (a SEPARATE operation — vouch
  // ≠ failure-reporting migration; operator decision). The definition
  // endpoints write back by FILENAME STEM (§9.7 finding #5): use _mStem (set
  // from the tile) and fall back to _mAppId for non-tile entry points, where
  // the endpoint's own stem-resolution covers any id/stem divergence.
  const _ds = m.definition_status || 'discovered';
  const _isDefined = _ds === 'defined';
  const _defStem = _mStem || _mAppId;
  const _driftAll = (m.drift_log || []).filter(e => e && e.source === 'scanner_drift');
  const _unrev = _driftAll.filter(e => e.reviewed === false).length;
  const _agentInvokes = (m.invocation_mode || 'agent_invokes') === 'agent_invokes';
  const _botLit = attrJsLiteral(_mBotId || '');
  const _defStemLit = attrJsLiteral(_defStem);
  const _defNameLit = attrJsLiteral(m.name || _mAppId);
  const _defStatusChip = _isDefined
    ? `<span class="badge badge-inline" style="font-size:0.65rem;background:var(--bg4);color:var(--green)">✓ defined</span>`
    : `<span class="badge badge-inline" style="font-size:0.65rem;background:var(--bg4);color:var(--text3)">discovered</span>`;
  const _defBtn = _isDefined
    ? `<button class="btn-soft" style="font-size:0.78rem" onclick="demoteDefinition(${_botLit},${_defStemLit},${_defNameLit})" title="Un-vouch — let the scanner manage this app as a draft again (reversible)">Demote to discovered</button>`
    : `<button class="btn-soft" style="font-size:0.78rem" onclick="promoteDefinition(${_botLit},${_defStemLit},${_defNameLit})" title="Vouch for this app — the scanner won't merge, rename, or auto-remove it">Promote to defined</button>`;
  const _defWhy = _isDefined
    ? `You've vouched for this app. The scanner keeps its details current but won't merge, rename, or auto-remove it — even if its files vanish.`
    : `The scanner inferred this app from your bot's workspace. Promote it to vouch it's real — then the scanner can only observe and narrate, never churn or delete it.`;
  const _driftLink = (_isDefined && _unrev > 0)
    ? `<div style="margin-top:8px"><button class="btn-soft" style="font-size:0.78rem;color:var(--blue)" onclick="openDriftPanel(${_botLit},${_defStemLit},${_defNameLit})">↻ ${_unrev} unreviewed change${_unrev === 1 ? '' : 's'} — review</button></div>`
    : '';
  // Narrowed by Bite 2a: failure-reporting honesty is now handled automatically
  // by the execution-integrity harness, so this cross-link no longer asserts the
  // app "misreports failures" (often false post-harness). It offers the SEPARATE
  // recognition-axis option — turning a genuinely event/channel-shaped app into
  // a deterministic hook — without pitching it as the misreport cure.
  const _reliabilityCrossLink = (_isDefined && _agentInvokes)
    ? `<div style="margin-top:10px;padding:10px 12px;border:1px solid var(--border);border-radius:6px;background:var(--bg3)">
        <div style="font-size:0.78rem;color:var(--text2);line-height:1.5">This app is vouched. Separately, if it's a <strong style="color:var(--text)">true event or channel hook</strong> — a message that should always run its script the same way — you can switch it to run automatically. (Failures are reported honestly either way.)
        <button class="btn-soft" style="font-size:0.76rem;margin-left:4px" onclick="openReliabilityModal(${_botLit},${attrJsLiteral(_mAppId)},${_defNameLit},true)">Run automatically…</button></div>
      </div>`
    : '';
  const definitionSection = `<div style="${secStyle}">
    ${secHead('Definition', 'Did the scanner guess this app, or have you vouched for it?')}
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px">${_defStatusChip}${_defBtn}</div>
    <div style="font-size:0.8rem;color:var(--text2);line-height:1.5">${esc(_defWhy)}</div>
    ${_driftLink}
    ${_reliabilityCrossLink}
  </div>`;

  document.getElementById('manifest-modal-inner').innerHTML =
    header + definitionSection + identitySection + successSection + constraintsSection + auditConfigSection + coherenceSection + triggersSection + satSection + histSection + issuesSection + versionDeltaSection + lessonsSection + filesSection;

  // Async: fetch file index and augment the files section in place
  if (m.pkg_id && _mBotId) {
    _fetchFileIndex(_mBotId, m.pkg_id, m.files || []);
  }

  // Async: fetch Lessons summary (v7-arc only)
  if (v7SpecId && _mBotId) {
    _fetchLessons(_mBotId, v7SpecId);
    _fetchVersionDelta(_mBotId, _mAppId, m.name);
  }
}

// Renders the Lessons body once /api/lessons/<bot>/<spec> returns.
// Renders the Lessons body. data is null for the 404 case (no Lessons file
// yet); otherwise data is the full /api/lessons/<bot>/<spec> response shape:
// {ok, lessons: {...full Lessons file...}, summary: {...}}. The full
// lessons[] array enables the expandable detail list.
function _renderLessonsBody(data, botId, specId) {
  const body = document.getElementById('manifest-lessons-body');
  if (!body) return;
  const esc = escHtml;

  if (data == null) {
    // Endpoint 404 — common post-migration before any compression has run.
    body.innerHTML = `<div style="font-size:0.78rem;color:var(--text3)">
      No Lessons file for this app yet. Run
      <code>python3 -m evolve_admin.applications.lessons_compress --bot-id ${esc(botId)}</code>
      to compress change_log entries once any have accumulated.
    </div>`;
    return;
  }

  const summary = data.summary || {};
  const fullLessons = (data.lessons && data.lessons.lessons) || [];

  const count = summary.lessons_count || 0;
  const kinds = summary.kinds || {};
  const ow    = summary.observation_window || {};
  const start = ow.start ? ow.start.replace('T', ' ').slice(0, 16) : '—';
  const end   = ow.end   ? ow.end.replace('T', ' ').slice(0, 16)   : '—';
  const runs  = ow.instance_runs != null ? ow.instance_runs : '—';

  const redactedBadge = summary.redaction_applied
    ? `<span class="badge badge-inline" style="font-size:0.65rem;background:var(--bg4);color:var(--green)">✓ redacted</span>`
    : `<span class="badge badge-inline" style="font-size:0.65rem;background:var(--bg4);color:var(--text3)">unredacted</span>`;

  const kindsLine = Object.keys(kinds).length
    ? Object.entries(kinds)
        .sort((a, b) => b[1] - a[1])
        .map(([k, n]) => `${esc(k)}=${n}`)
        .join(' · ')
    : 'none';

  // Spec privacy gate — schema §6 default is false. Toggle reads/writes
  // Spec.privacy.shareable_in_lessons via PATCH /api/applications/.../spec-privacy.
  // Only editable when the Spec lives in gallery/local (builtin + imported
  // are upstream-owned — need fork first).
  const specPrivacy = summary.spec_privacy;
  const specTier = summary.spec_tier;
  const allowShare = !!(specPrivacy && specPrivacy.shareable_in_lessons);
  let privacyToggle = '';
  if (specPrivacy != null) {
    const editable = specTier === 'local';
    const tip = editable
      ? 'Flip to permit / forbid Lessons share for this app. Required before Share Lessons works.'
      : `Read-only — Spec lives in gallery/${esc(specTier || 'unknown')}. Re-share this app from this bot to fork the Spec into gallery/local first.`;
    const disabledAttr = editable ? '' : 'disabled';
    privacyToggle = `<label style="display:inline-flex;align-items:center;gap:6px;font-size:0.72rem;color:var(--text2);margin-top:6px;${editable ? 'cursor:pointer' : 'opacity:0.6'}" title="${tip}">
      <input type="checkbox" ${allowShare ? 'checked' : ''} ${disabledAttr}
             onchange="applySpecPrivacyToggle('${esc(_mBotId || '')}','${esc(_mAppId || '')}', this.checked)"
             style="width:auto;margin:0">
      Allow Lessons share <span style="color:var(--text3)">(spec.privacy.shareable_in_lessons)</span>
    </label>`;
  }

  // Share button gated on BOTH having lessons AND the Spec privacy flag.
  let shareBtn;
  if (count === 0) {
    shareBtn = `<button class="btn btn-ghost btn-sm" disabled title="No Lessons to share yet" style="opacity:0.5">⇄ Share Lessons</button>`;
  } else if (!allowShare) {
    shareBtn = `<button class="btn btn-ghost btn-sm" disabled title="Spec doesn't permit Lessons share. Tick the box below to enable." style="opacity:0.5">⇄ Share Lessons</button>`;
  } else {
    shareBtn = `<button class="btn btn-ghost btn-sm" onclick="shareLessons('${esc(botId)}','${esc(specId)}')">⇄ Share Lessons</button>`;
  }

  // Per-Lesson expandable detail. Each row shows kind, summary, evidence
  // count. Collapsed by default since most operators just want the count.
  // Kind-icon palette matches the schema's enum (worked_well/failed/
  // new_capability/blueprint_correction).
  const _LESSON_KIND_META = {
    worked_well:           { icon: '✓', color: 'var(--green)' },
    failed:                { icon: '✗', color: 'var(--red)' },
    new_capability:        { icon: '＋', color: 'var(--blue)' },
    blueprint_correction:  { icon: '↻', color: 'var(--yellow)' },
  };
  const lessonsList = fullLessons.length > 0
    ? `<details style="margin-top:10px">
        <summary style="display:flex;align-items:center;gap:8px;font-size:0.78rem;color:var(--text2);cursor:pointer">
          <span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
          Show individual lessons (${fullLessons.length})
        </summary>
        <div style="margin-top:8px;display:flex;flex-direction:column;gap:6px">
          ${fullLessons.map(l => {
            const meta = _LESSON_KIND_META[l.kind] || {icon: '•', color: 'var(--text3)'};
            const evCount = (l.evidence || []).length;
            return `<div style="padding:6px 10px;background:var(--bg2);border-left:3px solid ${meta.color};border-radius:3px;font-size:0.78rem">
              <div style="margin-bottom:2px">
                <span style="color:${meta.color};font-weight:700">${meta.icon} ${esc(l.kind || 'unknown')}</span>
                <span style="color:var(--text3);font-size:0.7rem;margin-left:6px">${evCount} evidence ref${evCount === 1 ? '' : 's'}</span>
              </div>
              <div style="color:var(--text1)">${esc(l.summary || '(no summary)')}</div>
            </div>`;
          }).join('')}
        </div>
      </details>`
    : '';

  body.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <div style="font-size:0.85rem">
        <strong>${count}</strong> ${count === 1 ? 'lesson' : 'lessons'}
        ${redactedBadge}
      </div>
      ${shareBtn}
    </div>
    <div style="font-size:0.72rem;color:var(--text2);margin-bottom:4px">
      Kinds: <code>${kindsLine}</code>
    </div>
    <div style="font-size:0.72rem;color:var(--text3)">
      Observed ${esc(start)} → ${esc(end)} · ${esc(String(runs))} instance run${runs === 1 ? '' : 's'}
    </div>
    ${lessonsList}
    ${privacyToggle}`;
}

// Flip the bound Spec's shareable_in_lessons flag. Re-fetches Lessons on
// success so the toggle + Share button reflect the new state immediately.
async function applySpecPrivacyToggle(botId, appId, newValue) {
  if (!botId || !appId) return;
  try {
    const r = await fetch(`/api/applications/${encodeURIComponent(botId)}/${encodeURIComponent(appId)}/spec-privacy`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({shareable_in_lessons: newValue}),
    });
    const data = await r.json();
    if (!r.ok) {
      toast('✗ ' + (data.error || `Privacy update failed (HTTP ${r.status})`), 'err');
      // Re-fetch to bounce the toggle back to its actual server-side state
      const specId = (_mData && _mData.provenance && _mData.provenance.spec_id) || '';
      if (specId) _fetchLessons(botId, specId);
      return;
    }
    toast(`✓ Lessons share ${newValue ? 'enabled' : 'disabled'} for this Spec`, 'ok');
    const specId = (_mData && _mData.provenance && _mData.provenance.spec_id) || '';
    if (specId) _fetchLessons(botId, specId);
  } catch (e) {
    toast('✗ Privacy update failed: ' + e, 'err');
  }
}

async function _fetchLessons(botId, specId) {
  const snapBotId = _mBotId;
  const snapAppId = _mAppId;
  // Split fetch from render so a render-side ReferenceError doesn't masquerade
  // as "Failed to load" (PR #1671 — _relTime undef looked like an API failure).
  let data;
  let is404 = false;
  try {
    const r = await fetch(`/api/lessons/${encodeURIComponent(botId)}/${encodeURIComponent(specId)}`);
    // Guard against stale updates if user has navigated to a different app
    if (_mBotId !== snapBotId || _mAppId !== snapAppId) return;
    if (r.status === 404) {
      is404 = true;
    } else if (!r.ok) {
      const body = document.getElementById('manifest-lessons-body');
      if (body) {
        body.innerHTML = `<div style="font-size:0.78rem;color:var(--yellow)">Failed to load Lessons (HTTP ${r.status})</div>`;
      }
      return;
    } else {
      data = await r.json();
    }
  } catch (e) {
    const body = document.getElementById('manifest-lessons-body');
    if (body) {
      body.innerHTML = `<div style="font-size:0.78rem;color:var(--yellow)">Failed to load Lessons: ${escHtml(String(e))}</div>`;
    }
    return;
  }
  _renderLessonsBody(is404 ? null : data, botId, specId);
}

async function shareLessons(botId, specId) {
  // POST /api/lessons/<bot>/<spec>/share applies redaction and writes the
  // shared copy to {shared_dir}/lessons-shared/<bot>/<spec>.json. Surface the
  // redaction_kind list so the operator knows what was transformed.
  try {
    const r = await fetch(`/api/lessons/${encodeURIComponent(botId)}/${encodeURIComponent(specId)}/share`, {
      method: 'POST',
    });
    const data = await r.json();
    if (!r.ok) {
      toast('✗ ' + (data.error || `Share failed (HTTP ${r.status})`), 'err');
      return;
    }
    const kinds = (data.redaction_kind || []).join(', ') || 'none';
    toast(`✓ Lessons redacted (${kinds}) → ${data.output_path}`, 'ok');
    // Refresh the section so the redacted badge flips to ✓
    _fetchLessons(botId, specId);
  } catch (e) {
    toast('✗ Share failed: ' + e, 'err');
  }
}

// Fetch the adopt-preview for the current Instance to surface drift in the
// View Manifest modal. Hides the section when current == latest (no drift)
// or when the preview can't be computed. Shows current → latest with a
// kind banner and a "Review & Adopt" deeplink to the existing Adopt modal.
async function _fetchVersionDelta(botId, appId, appName) {
  const snapBotId = _mBotId;
  const snapAppId = _mAppId;
  const section = document.getElementById('manifest-version-delta-section');
  const body = document.getElementById('manifest-version-delta-body');
  if (!section || !body) return;

  try {
    const r = await fetch(`/api/applications/${encodeURIComponent(botId)}/${encodeURIComponent(appId)}/adopt-preview`);
    if (_mBotId !== snapBotId || _mAppId !== snapAppId) return;
    if (!r.ok) {
      // 400 here likely means no Spec in gallery or some other resolvable
      // edge case — leave the section hidden rather than show a confusing
      // error in a polish surface.
      return;
    }
    const data = await r.json();

    // Same version → no drift to surface. Hide the section.
    if (data.from_version === data.to_version) {
      return;
    }

    // Show the section + render
    section.style.display = '';
    const diff = data.spec_diff || {};
    const kind = diff.kind || 'unknown';
    let banner;
    if (kind === 'presentation_only') {
      banner = `<div style="font-size:0.78rem;color:var(--green);padding:6px 10px;background:var(--bg2);border-radius:4px;margin:6px 0">
        ✓ Presentation-only diff — safe to adopt with pointer-only rebind.
      </div>`;
    } else if (kind === 'structural') {
      const struct = (diff.structural_fields_touched || []).join(', ') || '(unspecified)';
      banner = `<div style="font-size:0.78rem;color:var(--red);padding:6px 10px;background:var(--bg2);border-left:3px solid var(--red);border-radius:4px;margin:6px 0">
        ⚠ Structural changes: ${escHtml(struct)}. Needs Forge rebuild — Adopt v1 will refuse.
      </div>`;
    } else {
      banner = `<div style="font-size:0.78rem;color:var(--yellow);padding:6px 10px;background:var(--bg2);border-radius:4px;margin:6px 0">
        Diff classification: ${escHtml(kind)}
      </div>`;
    }

    const fieldsLine = (() => {
      const ch = (diff.fields_changed || []).length;
      const ad = (diff.fields_added || []).length;
      const rm = (diff.fields_removed || []).length;
      const parts = [];
      if (ch) parts.push(`${ch} changed`);
      if (ad) parts.push(`${ad} added`);
      if (rm) parts.push(`${rm} removed`);
      return parts.length ? `<div style="font-size:0.72rem;color:var(--text2)">Fields: ${parts.join(' · ')}</div>` : '';
    })();

    body.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div style="font-size:0.85rem">
          <code>${escHtml(data.from_version)}</code>
          <span style="color:var(--text3);margin:0 6px">→</span>
          <code>${escHtml(data.to_version)}</code>
        </div>
        <button class="btn btn-ghost btn-sm" onclick="openAdoptModal('${escHtml(botId)}','${escHtml(appId)}','${escHtml(appName || appId)}')">Review & Adopt →</button>
      </div>
      ${banner}
      ${fieldsLine}
    `;
  } catch (e) {
    // Silently leave section hidden — version-delta is a polish surface,
    // not load-bearing.
  }
}

// ── Coherence + Drift section + action handlers ──────────────────────────
// Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §10–§11.
//
// Reads manifest.coherence + manifest.reconciliation. Groups findings per
// the same buckets the bot-side session_surface and the evo
// app_changes_renderer use, so the operator and the bot see the same
// vocabulary. Action buttons (Approve / Promote / Flag / Mute / Snooze /
// Override) POST to the /api/applications/<bot>/<app>/... routes added in
// server.py and then refresh the modal in place.

function _renderCoherenceSection(m, secStyle, secHead, esc) {
  const coh = m.coherence || {};
  const rec = m.reconciliation || {};
  const findings = Array.isArray(coh.findings) ? coh.findings : [];
  const accepted = Array.isArray(coh.coherence_accepted) ? coh.coherence_accepted : [];
  const acceptedSigs = new Set(accepted.map(a => (a && a.signature) || '').filter(Boolean));
  const flags = Array.isArray(coh.flags) ? coh.flags : [];
  const lastCap = coh.last_capability_check || null;
  const nowIso = new Date().toISOString();

  // Live = not muted, not snoozed-future.
  const isLive = (f) => {
    const sig = f.signature || f.id || '';
    if (sig && acceptedSigs.has(sig)) return false;
    const until = (f.snooze && f.snooze.until) || '';
    if (until && until > nowIso) return false;
    return true;
  };
  const liveFindings = findings.filter(isLive);
  const snoozedFindings = findings.filter(f => {
    const until = (f.snooze && f.snooze.until) || '';
    return until && until > nowIso;
  });

  // Bucket findings the same way session_surface_apps.py does.
  const QUIET_ASSERTIONS = new Set([
    'recurring_behavior_without_trigger',
    'recurring_behavior_only_suspect_actions',
    'openclaw_cron_run_status',
  ]);
  const quietFailures = liveFindings.filter(f => QUIET_ASSERTIONS.has(f.assertion || ''));
  const passAFindings = liveFindings.filter(f =>
    (f.id || '').startsWith('C-A') && !QUIET_ASSERTIONS.has(f.assertion || '')
  );
  const passC1Findings = liveFindings.filter(f => (f.id || '').startsWith('C1-'));
  const otherFindings = liveFindings.filter(f =>
    !QUIET_ASSERTIONS.has(f.assertion || '') &&
    !(f.id || '').startsWith('C-A') &&
    !(f.id || '').startsWith('C1-')
  );

  const drifted = Array.isArray(rec.drifted_fields) ? rec.drifted_fields : [];
  const added = Array.isArray(rec.added_files) ? rec.added_files : [];
  const removed = Array.isArray(rec.removed_files) ? rec.removed_files : [];
  const recStatus = (rec.status || 'ok').toLowerCase();
  const isOrphan = recStatus === 'orphan';
  const hasDrift = drifted.length + added.length + removed.length > 0;
  const hasAny = (
    quietFailures.length + passAFindings.length + passC1Findings.length +
    otherFindings.length + drifted.length + added.length + removed.length +
    (lastCap ? 1 : 0) + (isOrphan ? 1 : 0) + flags.length + accepted.length
  ) > 0;

  // Header chip — same logic as the cap-card pill so the modal echoes
  // the list view.
  const cohStatus = coh.status || 'ok';
  let headerChip;
  if (isOrphan) {
    headerChip = '<span style="color:var(--red);font-weight:600">▼ orphan</span>';
  } else if (cohStatus === 'incoherent') {
    headerChip = '<span style="color:var(--red);font-weight:600">✗ incoherent</span>';
  } else if (cohStatus === 'warnings') {
    headerChip = '<span style="color:var(--yellow);font-weight:600">⚠ warnings</span>';
  } else if (hasDrift) {
    headerChip = '<span style="color:var(--blue);font-weight:600">↻ drift</span>';
  } else {
    headerChip = '<span style="color:var(--green);font-weight:600">✓ ok</span>';
  }

  if (!hasAny) {
    return `<div id="m-coherence-section" style="${secStyle}">
      ${secHead('Coherence + Drift', 'Does the manifest match what the bot can actually do? See docs/spec-app-coherence-and-reconciliation-2026-06-05.md.')}
      <div style="display:flex;align-items:center;gap:10px">
        ${headerChip}
        <span style="color:var(--text2);font-size:0.8rem">No findings — manifest is consistent with disk and internally coherent.</span>
      </div>
    </div>`;
  }

  // Action buttons row. All actions hit /api/applications/<bot>/<app>/...
  // and call refreshManifestInPlace() on success so the modal updates
  // without a page reload.
  const hasLiveFindings = (
    quietFailures.length + passAFindings.length + passC1Findings.length +
    otherFindings.length
  ) > 0;
  const repairBtn = hasLiveFindings
    ? `<button class="btn-soft" style="font-size:0.78rem" onclick="repairChatOpen()" title="Open a conversation with ${esc(_mBotId)} about these findings. The bot's LLM sees the manifest + findings and proposes fixes you approve.">💬 Repair with ${esc(_mBotId)}…</button>`
    : '';
  const actionRow = `<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px">
    <button class="btn-soft" style="font-size:0.78rem" onclick="appCoherenceApprove()" ${hasDrift ? '' : 'disabled'} title="Accept the current observational state — clears reconciliation drift">Approve drift</button>
    <button class="btn-soft" style="font-size:0.78rem" onclick="appCoherencePromote()" title="Flip observational provenance entries to bot_authored">Promote to authored</button>
    <button class="btn-soft" style="font-size:0.78rem" onclick="appCoherenceOpenFlag()" title="Escalate a concern to the operator">Flag…</button>
    ${repairBtn}
    ${cohStatus === 'incoherent' ? `<button class="btn-soft" style="font-size:0.78rem;color:var(--red)" onclick="appCoherenceOpenOverride()" title="Bypass the pre-deploy gate for the current finding set">Override pre-deploy gate…</button>` : ''}
  </div>`;

  // Renders one finding row with Mute + Snooze affordances.
  const sevColor = (sev) => ({
    critical: 'var(--red)', major: 'var(--red)',
    minor:    'var(--yellow)', info: 'var(--text3)',
  })[(sev || '').toLowerCase()] || 'var(--text3)';

  const findingRow = (f) => {
    const sig = f.signature || f.id || '';
    const sev = (f.severity || '').toLowerCase();
    const msg = f.description || f.message || f.summary || (f.id || '(unnamed finding)');
    const hint = f.repair_hint || '';
    const assertion = f.assertion || '';
    return `<li style="margin-bottom:6px;font-size:0.8rem;line-height:1.35">
      <span style="color:${sevColor(sev)};font-weight:600">${esc(sev || 'note')}</span>
      ${assertion ? ` <code style="font-size:0.7rem;color:var(--text3)">${esc(assertion)}</code>` : ''}
      <span style="color:var(--text1);margin-left:4px">${esc(msg)}</span>
      ${hint ? `<div style="font-size:0.72rem;color:var(--text3);margin-top:2px">↳ ${esc(hint)}</div>` : ''}
      ${sig ? `<div style="margin-top:3px;display:flex;gap:6px;align-items:center">
        <a href="#" style="font-size:0.7rem;color:var(--text3)" onclick="appCoherenceMute('${esc(sig)}'); return false">Mute</a>
        <span style="color:var(--text3);font-size:0.7rem">·</span>
        <a href="#" style="font-size:0.7rem;color:var(--text3)" onclick="appCoherenceSnooze('${esc(sig)}'); return false">Snooze 7d</a>
        <span style="font-size:0.65rem;color:var(--text3);font-family:monospace">${esc(sig.slice(0, 12))}</span>
      </div>` : ''}
    </li>`;
  };

  // ── Quiet failures ────────────────────────────────────────────
  const quietBlock = quietFailures.length > 0 ? `
    <div style="margin-bottom:12px">
      <div style="font-size:0.73rem;font-weight:700;color:var(--text2);margin-bottom:4px">Quiet failures (${quietFailures.length})</div>
      <div style="font-size:0.72rem;color:var(--text3);margin-bottom:6px">Behaviors the manifest claims, with no firing mechanism — the cron that doesn't fire, the script that runs but produces nothing.</div>
      <ul style="margin:0;padding-left:18px">${quietFailures.map(findingRow).join('')}</ul>
    </div>` : '';

  // ── Coherence (Pass A + Pass C1) ─────────────────────────────
  const cohBlock = (passAFindings.length + passC1Findings.length + otherFindings.length) > 0 ? `
    <div style="margin-bottom:12px">
      <div style="font-size:0.73rem;font-weight:700;color:var(--text2);margin-bottom:4px">Coherence findings (${passAFindings.length + passC1Findings.length + otherFindings.length})</div>
      <div style="font-size:0.72rem;color:var(--text3);margin-bottom:6px">Pass A walks the manifest graph (description claims a behavior → does a mechanism produce it?). Pass C1 checks code shape statically.</div>
      ${passAFindings.length > 0 ? `<div style="font-size:0.7rem;font-weight:600;color:var(--text3);margin-bottom:2px">Pass A (manifest graph)</div>
        <ul style="margin:0 0 6px;padding-left:18px">${passAFindings.map(findingRow).join('')}</ul>` : ''}
      ${passC1Findings.length > 0 ? `<div style="font-size:0.7rem;font-weight:600;color:var(--text3);margin-bottom:2px">Pass C1 (code shape)</div>
        <ul style="margin:0 0 6px;padding-left:18px">${passC1Findings.map(findingRow).join('')}</ul>` : ''}
      ${otherFindings.length > 0 ? `<div style="font-size:0.7rem;font-weight:600;color:var(--text3);margin-bottom:2px">Other</div>
        <ul style="margin:0;padding-left:18px">${otherFindings.map(findingRow).join('')}</ul>` : ''}
    </div>` : '';

  // ── Capability check (Pass C3 — LLM verdict cached on manifest) ──────
  let capBlock = '';
  if (lastCap && typeof lastCap === 'object') {
    const sev = (lastCap.severity || lastCap.verdict || '').toLowerCase();
    const sevLabel = ({feasible: '✓ feasible', unclear: '? unclear', incoherent: '✗ incoherent'})[sev] || (sev || '?');
    const sevCol = sev === 'feasible' ? 'var(--green)' : sev === 'incoherent' ? 'var(--red)' : 'var(--yellow)';
    const checkedAt = lastCap.checked_at || '';
    capBlock = `
    <div style="margin-bottom:12px">
      <div style="font-size:0.73rem;font-weight:700;color:var(--text2);margin-bottom:4px">Capability check (Pass C3)</div>
      <div style="font-size:0.8rem"><span style="color:${sevCol};font-weight:600">${esc(sevLabel)}</span>${checkedAt ? `<span style="color:var(--text3);font-size:0.72rem;margin-left:8px">${esc(checkedAt.slice(0, 10))}</span>` : ''}</div>
      ${lastCap.rationale ? `<div style="font-size:0.78rem;color:var(--text2);margin-top:4px">${esc(lastCap.rationale)}</div>` : ''}
    </div>`;
  }

  // ── Reconciliation drift ─────────────────────────────────────
  let reconBlock = '';
  if (hasDrift || isOrphan) {
    const driftedRows = drifted.slice(0, 6).map(d => {
      const fieldName = d.field || d.path || '?';
      const authored = d.authored ? '<span style="color:var(--blue);font-weight:600">authored</span> ' : '';
      const before = JSON.stringify(d.before);
      const after = JSON.stringify(d.after);
      return `<li style="margin-bottom:3px;font-size:0.78rem">${authored}<code style="font-size:0.72rem">${esc(fieldName)}</code>: ${esc(before)} → ${esc(after)}</li>`;
    }).join('');
    const addedRows = added.slice(0, 6).map(a => `<li style="margin-bottom:2px;font-size:0.76rem;color:var(--green)">+ ${esc(a.path || a)}</li>`).join('');
    const removedRows = removed.slice(0, 6).map(r => `<li style="margin-bottom:2px;font-size:0.76rem;color:var(--red)">– ${esc(r.path || r)}</li>`).join('');
    reconBlock = `
    <div style="margin-bottom:12px">
      <div style="font-size:0.73rem;font-weight:700;color:var(--text2);margin-bottom:4px">Reconciliation drift${isOrphan ? ' — <span style=\"color:var(--red)\">orphan</span>' : ''}</div>
      <div style="font-size:0.72rem;color:var(--text3);margin-bottom:6px">What's on disk differs from what the manifest claims. Approve to accept current state; promote to upgrade observational fields to authored.</div>
      ${drifted.length > 0 ? `<div style="font-size:0.7rem;font-weight:600;color:var(--text3);margin-bottom:2px">Drifted fields (${drifted.length})</div>
        <ul style="margin:0 0 6px;padding-left:18px">${driftedRows}${drifted.length > 6 ? `<li style="font-size:0.72rem;color:var(--text3)">+${drifted.length - 6} more</li>` : ''}</ul>` : ''}
      ${added.length > 0 ? `<div style="font-size:0.7rem;font-weight:600;color:var(--text3);margin-bottom:2px">Added files (${added.length})</div>
        <ul style="margin:0 0 6px;padding-left:18px;list-style:none">${addedRows}${added.length > 6 ? `<li style="font-size:0.72rem;color:var(--text3)">+${added.length - 6} more</li>` : ''}</ul>` : ''}
      ${removed.length > 0 ? `<div style="font-size:0.7rem;font-weight:600;color:var(--text3);margin-bottom:2px">Removed files (${removed.length})</div>
        <ul style="margin:0;padding-left:18px;list-style:none">${removedRows}${removed.length > 6 ? `<li style="font-size:0.72rem;color:var(--text3)">+${removed.length - 6} more</li>` : ''}</ul>` : ''}
    </div>`;
  }

  // ── Flags ─────────────────────────────────────────────────────
  const flagBlock = flags.length > 0 ? `
    <div style="margin-bottom:12px">
      <div style="font-size:0.73rem;font-weight:700;color:var(--text2);margin-bottom:4px">Flagged for operator (${flags.length})</div>
      <ul style="margin:0;padding-left:18px">${flags.slice(0, 5).map(f => `<li style="margin-bottom:3px;font-size:0.78rem"><span style="color:var(--text1)">${esc(f.description || '')}</span><span style="color:var(--text3);font-size:0.7rem;margin-left:6px">${esc((f.at || '').slice(0, 10))}</span></li>`).join('')}</ul>
    </div>` : '';

  // ── Muted + snoozed ─────────────────────────────────────────
  const mutedBlock = (accepted.length + snoozedFindings.length) > 0 ? `
    <div style="margin-top:8px;padding-top:8px;border-top:1px dashed var(--border);font-size:0.72rem;color:var(--text3)">
      ${accepted.length > 0 ? `<div>${accepted.length} muted finding${accepted.length === 1 ? '' : 's'} — <a href="#" onclick="appCoherenceShowMuted(); return false" style="color:var(--text3)">view</a></div>` : ''}
      ${snoozedFindings.length > 0 ? `<div>${snoozedFindings.length} snoozed</div>` : ''}
    </div>` : '';

  return `<div id="m-coherence-section" style="${secStyle}">
    ${secHead('Coherence + Drift', 'Does the manifest match what the bot can actually do? See docs/spec-app-coherence-and-reconciliation-2026-06-05.md.')}
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px;font-size:0.8rem">
      ${headerChip}
      <span style="color:var(--text3)">${liveFindings.length} active finding${liveFindings.length === 1 ? '' : 's'}</span>
    </div>
    ${actionRow}
    ${quietBlock}
    ${cohBlock}
    ${capBlock}
    ${reconBlock}
    ${flagBlock}
    ${mutedBlock}
  </div>`;
}

// ── Coherence action handlers ────────────────────────────────────
//
// Each handler hits the matching /api/applications/<bot>/<app>/... route,
// then re-fetches the manifest and re-renders the modal so the UI
// reflects post-action state without a full page reload.

async function _refreshCoherenceModal() {
  // Same as viewManifest but assumes the modal is already open — just
  // reload the data and re-render in place.
  const m = await api('GET', `/api/applications/${_mBotId}/${_mAppId}`);
  if (m.error) { toast('✗ ' + m.error, 'err'); return; }
  _mData = m;
  _renderManifestModal();
}

async function appCoherenceApprove() {
  if (!_mBotId || !_mAppId) return;
  const r = await api('POST', `/api/applications/${_mBotId}/${_mAppId}/approve`);
  if (r.ok) {
    toast(`✓ Approved ${r.approved_count || 0} change${r.approved_count === 1 ? '' : 's'}`, 'ok');
    await _refreshCoherenceModal();
  } else {
    toast('✗ ' + (r.error || 'approve failed'), 'err');
  }
}

async function appCoherencePromote() {
  if (!_mBotId || !_mAppId) return;
  const r = await api('POST', `/api/applications/${_mBotId}/${_mAppId}/promote`);
  if (r.ok) {
    const n = (r.promoted_fields || []).length;
    toast(n ? `✓ Promoted ${n} field${n === 1 ? '' : 's'} to authored` : 'Nothing to promote', n ? 'ok' : 'warn');
    await _refreshCoherenceModal();
  } else {
    toast('✗ ' + (r.error || 'promote failed'), 'err');
  }
}

function appCoherenceOpenFlag() {
  if (!_mBotId || !_mAppId) return;
  const modal = document.createElement('div');
  modal.id = 'coh-flag-modal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.55);display:flex;align-items:center;justify-content:center';
  modal.innerHTML = `<div style="background:var(--bg2);border-radius:12px;padding:18px 20px;width:90%;max-width:480px;border:1px solid var(--border)">
    <div style="font-weight:700;font-size:0.95rem;margin-bottom:8px">Flag a concern for the operator</div>
    <div style="font-size:0.78rem;color:var(--text2);margin-bottom:10px">A short description of what's wrong. The flag lands on this app's manifest and surfaces in the bot's session-start block.</div>
    <textarea id="coh-flag-text" rows="4" placeholder="What's off? Example: cron didn't fire last night, output empty since Tuesday, …" style="width:100%;font-size:0.85rem;font-family:inherit;background:var(--bg1);border:1px solid var(--border);border-radius:6px;padding:8px 10px;color:var(--text1);resize:vertical"></textarea>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px">
      <button class="btn btn-ghost btn-sm" onclick="document.getElementById('coh-flag-modal').remove()">Cancel</button>
      <button class="btn btn-primary btn-sm" onclick="appCoherenceSubmitFlag()">Submit flag</button>
    </div>
  </div>`;
  document.body.appendChild(modal);
  setTimeout(() => { const t = document.getElementById('coh-flag-text'); if (t) t.focus(); }, 30);
}

async function appCoherenceSubmitFlag() {
  const t = document.getElementById('coh-flag-text');
  const description = (t && t.value || '').trim();
  if (!description) { toast('Description required', 'err'); return; }
  const r = await api('POST', `/api/applications/${_mBotId}/${_mAppId}/flag`, {description});
  if (r.ok) {
    toast('✓ Flag recorded', 'ok');
    const modal = document.getElementById('coh-flag-modal');
    if (modal) modal.remove();
    await _refreshCoherenceModal();
  } else {
    toast('✗ ' + (r.error || 'flag failed'), 'err');
  }
}

async function appCoherenceMute(signature) {
  if (!_mBotId || !_mAppId || !signature) return;
  const rationale = prompt('Why mute this finding? (optional)') || '';
  const r = await api('POST', `/api/applications/${_mBotId}/${_mAppId}/coherence/mute`, {signature, rationale});
  if (r.ok) {
    toast('✓ Finding muted', 'ok');
    await _refreshCoherenceModal();
  } else {
    toast('✗ ' + (r.error || 'mute failed'), 'err');
  }
}

async function appCoherenceUnmute(signature) {
  if (!_mBotId || !_mAppId || !signature) return;
  const r = await api('POST', `/api/applications/${_mBotId}/${_mAppId}/coherence/unmute`, {signature});
  if (r.ok) {
    toast('Finding un-muted', 'ok');
    await _refreshCoherenceModal();
  } else {
    toast('✗ ' + (r.error || 'unmute failed'), 'err');
  }
}

async function appCoherenceSnooze(signature) {
  if (!_mBotId || !_mAppId || !signature) return;
  const r = await api('POST', `/api/applications/${_mBotId}/${_mAppId}/coherence/snooze`, {signature});
  if (r.ok) {
    toast(`✓ Snoozed until ${(r.until || '').slice(0, 10)}`, 'ok');
    await _refreshCoherenceModal();
  } else {
    toast('✗ ' + (r.error || 'snooze failed'), 'err');
  }
}

function appCoherenceShowMuted() {
  const m = _mData || {};
  const accepted = (m.coherence || {}).coherence_accepted || [];
  if (!accepted.length) { toast('No muted findings', 'warn'); return; }
  const modal = document.createElement('div');
  modal.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.55);display:flex;align-items:center;justify-content:center';
  modal.innerHTML = `<div style="background:var(--bg2);border-radius:12px;padding:18px 20px;width:90%;max-width:600px;border:1px solid var(--border);max-height:80vh;overflow:auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <div style="font-weight:700;font-size:0.95rem">Muted findings (${accepted.length})</div>
      <button class="btn btn-ghost btn-sm" onclick="this.closest('[style*=fixed]').remove()">✕</button>
    </div>
    <ul style="margin:0;padding-left:18px;font-size:0.8rem">
      ${accepted.map(a => `<li style="margin-bottom:6px">
        <code style="font-size:0.7rem;color:var(--text3)">${escHtml((a.signature || '').slice(0, 16))}</code>
        ${a.rationale ? `<div style="font-size:0.75rem;color:var(--text2);margin-top:2px">${escHtml(a.rationale)}</div>` : ''}
        <a href="#" onclick="appCoherenceUnmute('${escHtml(a.signature || '')}'); this.closest('[style*=fixed]').remove(); return false" style="font-size:0.7rem;color:var(--text3)">un-mute</a>
      </li>`).join('')}
    </ul>
  </div>`;
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  document.body.appendChild(modal);
}

async function appCoherenceOpenOverride() {
  if (!_mBotId || !_mAppId) return;
  const v = await api('GET', `/api/applications/${_mBotId}/${_mAppId}/pre-deploy-verdict`);
  if (!v.ok && v.ok !== undefined) { toast('✗ ' + (v.error || 'gate check failed'), 'err'); return; }
  const overrideKey = v.override_key || '';
  const n = (v.findings || []).length;
  const modal = document.createElement('div');
  modal.id = 'coh-override-modal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.55);display:flex;align-items:center;justify-content:center';
  modal.innerHTML = `<div style="background:var(--bg2);border-radius:12px;padding:18px 20px;width:90%;max-width:540px;border:1px solid var(--red)">
    <div style="font-weight:700;font-size:0.95rem;color:var(--red);margin-bottom:8px">Override pre-deploy coherence gate</div>
    <div style="font-size:0.8rem;color:var(--text2);margin-bottom:8px">Pass A found ${n} finding${n === 1 ? '' : 's'} on this manifest. Forge approval would normally refuse this build because the bot can't deliver what the manifest claims.</div>
    <div style="font-size:0.8rem;color:var(--text2);margin-bottom:12px">Override ships anyway. The bot will likely fall short of what users ask of this app until the underlying issue is fixed. Type the override key below to confirm:</div>
    <div style="font-family:monospace;font-size:0.85rem;background:var(--bg1);border:1px solid var(--border);border-radius:5px;padding:6px 10px;color:var(--text1);margin-bottom:10px;user-select:all">${escHtml(overrideKey)}</div>
    <input id="coh-override-input" placeholder="Re-type the override key" style="width:100%;font-family:monospace;font-size:0.85rem;background:var(--bg1);border:1px solid var(--border);border-radius:5px;padding:6px 10px;color:var(--text1);margin-bottom:12px">
    <div style="display:flex;justify-content:flex-end;gap:8px">
      <button class="btn btn-ghost btn-sm" onclick="document.getElementById('coh-override-modal').remove()">Cancel</button>
      <button class="btn btn-danger btn-sm" onclick="appCoherenceSubmitOverride('${escHtml(overrideKey)}')">Ship anyway</button>
    </div>
  </div>`;
  document.body.appendChild(modal);
  setTimeout(() => { const t = document.getElementById('coh-override-input'); if (t) t.focus(); }, 30);
}

function appCoherenceSubmitOverride(expectedKey) {
  const t = document.getElementById('coh-override-input');
  const typed = (t && t.value || '').trim();
  if (typed !== expectedKey) { toast('Override key mismatch — type it exactly', 'err'); return; }
  // The override is consumed by the next forge approval / manifest editor save
  // — we stash it on the manifest so the next save sends it through. The
  // gate compares against the live override_key for the current findings.
  window._coherenceOverrideKey = expectedKey;
  toast('✓ Override key armed — next save / approve will ship', 'ok');
  const modal = document.getElementById('coh-override-modal');
  if (modal) modal.remove();
}

// ── Repair-chat modal ─────────────────────────────────────────────────────
//
// Opens a chat with the current app's bot LLM. The bot has the manifest +
// findings as context and emits structured proposals; the operator must
// explicitly click Apply on each one. Backed by
// /api/applications/<bot>/<app>/repair-chat/{state,message,apply,end}.
// Spec: handler module is packages/admin/evolve_admin/applications/repair_chat.py.

let _repairChatBotId = null;
let _repairChatAppId = null;
let _repairChatBusy = false;
const _repairChatAppliedIds = new Set();

async function repairChatOpen() {
  if (!_mBotId || !_mAppId) return;
  _repairChatBotId = _mBotId;
  _repairChatAppId = _mAppId;
  _repairChatAppliedIds.clear();
  // Fetch initial state — log + lock + usage. This also acquires the
  // cookie (Set-Cookie comes back on first hit).
  let state;
  try {
    state = await api('GET', `/api/applications/${_mBotId}/${_mAppId}/repair-chat/state`);
  } catch (e) {
    toast('✗ Could not open repair chat: ' + e, 'err');
    return;
  }
  if (!state || !state.ok) {
    toast('✗ ' + ((state && state.error) || 'repair chat unavailable'), 'err');
    return;
  }
  const lockedByOther = state.lock && state.lock.locked && state.lock.owner && state.lock.owner !== state.operator_id;

  // Mount the modal shell.
  const modal = document.createElement('div');
  modal.id = 'repair-chat-modal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.55);display:flex;align-items:center;justify-content:center';
  modal.innerHTML = `
    <div style="background:var(--bg2);border-radius:12px;padding:0;width:92%;max-width:680px;max-height:88vh;display:flex;flex-direction:column;border:1px solid var(--border)">
      <div style="display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid var(--border)">
        <div>
          <div style="font-weight:700;font-size:0.95rem">💬 Repair with ${escHtml(_repairChatBotId)}</div>
          <div style="font-size:0.72rem;color:var(--text3);margin-top:2px">App: <code>${escHtml(_repairChatAppId)}</code> · proposed changes need your explicit Apply click</div>
        </div>
        <button class="btn btn-ghost btn-sm" onclick="repairChatClose()" title="Release the chat lock and close">✕</button>
      </div>
      <div id="repair-chat-banner" style="padding:8px 18px;font-size:0.75rem;color:var(--text2);border-bottom:1px solid var(--border);background:var(--bg1)"></div>
      <div id="repair-chat-log" style="flex:1;overflow-y:auto;padding:14px 18px;display:flex;flex-direction:column;gap:12px"></div>
      <div style="padding:10px 18px;border-top:1px solid var(--border);background:var(--bg1)">
        <div style="display:flex;gap:8px;align-items:flex-end">
          <textarea id="repair-chat-input" rows="2" placeholder="Ask ${escHtml(_repairChatBotId)} about a finding, or describe what you want fixed…" style="flex:1;font-size:0.85rem;font-family:inherit;background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:8px 10px;color:var(--text1);resize:vertical;min-height:42px"
            onkeydown="if(event.key==='Enter' && (event.ctrlKey || event.metaKey)){event.preventDefault();repairChatSend();}"></textarea>
          <button class="btn btn-primary btn-sm" id="repair-chat-send" onclick="repairChatSend()">Send</button>
        </div>
        <div id="repair-chat-meta" style="font-size:0.7rem;color:var(--text3);margin-top:6px">⌘/Ctrl-Enter to send · ≈ $${(state.estimated_cost_per_turn_usd || 0.02).toFixed(2)} per turn</div>
      </div>
    </div>`;
  modal.addEventListener('click', e => { if (e.target === modal) repairChatClose(); });
  document.body.appendChild(modal);

  _repairChatRenderState(state);

  if (lockedByOther) {
    const banner = document.getElementById('repair-chat-banner');
    const expires = (state.lock.expires_at || '').slice(11, 16);
    banner.innerHTML = `<span style="color:var(--orange)">⚠ Another operator is repair-chatting this app right now${expires ? ` (lock expires at ${expires} UTC)` : ''}. You can read the history; sending a message will be blocked.</span>`;
    const input = document.getElementById('repair-chat-input');
    const send = document.getElementById('repair-chat-send');
    if (input) input.disabled = true;
    if (send) send.disabled = true;
  } else {
    setTimeout(() => { const t = document.getElementById('repair-chat-input'); if (t) t.focus(); }, 30);
  }
}

function _repairChatRenderState(state) {
  // Render usage chip + the existing log entries (oldest first).
  const u = state.usage || {};
  const meta = document.getElementById('repair-chat-meta');
  if (meta) {
    const cap = u.daily_cap || 10;
    const used = u.used || 0;
    const remaining = u.remaining != null ? u.remaining : (cap - used);
    const capCol = remaining <= 2 ? 'var(--orange)' : 'var(--text3)';
    meta.innerHTML = `⌘/Ctrl-Enter to send · ≈ $${(state.estimated_cost_per_turn_usd || 0.02).toFixed(2)} per turn · <span style="color:${capCol}">${used}/${cap} messages used today</span>`;
  }

  // Filter the chat log into displayable turns. Apply rows render as
  // muted status lines so the audit trail is visible without dominating
  // the conversation.
  const log = Array.isArray(state.log) ? state.log : [];
  // Track which proposal_ids have been applied so the cards know to
  // render as locked-in.
  _repairChatAppliedIds.clear();
  for (const e of log) {
    if ((e.role || '') === 'apply' && e.proposal_id) {
      _repairChatAppliedIds.add(e.proposal_id);
    }
  }

  const logEl = document.getElementById('repair-chat-log');
  if (!logEl) return;
  logEl.innerHTML = log.map(_repairChatRenderTurn).filter(Boolean).join('');
  logEl.scrollTop = logEl.scrollHeight;

  if (log.length === 0) {
    // First-open prompt — operator hasn't sent anything yet.
    logEl.innerHTML = `<div style="font-size:0.8rem;color:var(--text3);text-align:center;padding:24px 12px">
      No conversation yet. Say something like <em>"walk me through the findings"</em> or paste a specific finding's signature to dig in.
    </div>`;
  }
}

function _repairChatRenderTurn(entry) {
  const role = (entry.role || '').toLowerCase();
  const ts = (entry.ts || '').slice(11, 16);
  if (role === 'user') {
    return `<div style="align-self:flex-end;max-width:80%;background:var(--accent-bg,rgba(80,120,200,0.15));border:1px solid var(--border);border-radius:10px 10px 2px 10px;padding:8px 12px">
      <div style="font-size:0.68rem;color:var(--text3);margin-bottom:3px">Operator · ${escHtml(ts)}</div>
      <div style="font-size:0.85rem;white-space:pre-wrap">${escHtml(entry.text || '')}</div>
    </div>`;
  }
  if (role === 'assistant') {
    const proposals = Array.isArray(entry.proposals) ? entry.proposals : [];
    const errBlock = entry.error
      ? `<div style="font-size:0.72rem;color:var(--orange);margin-top:6px;border-top:1px dashed var(--border);padding-top:5px"><code>${escHtml(entry.error)}</code></div>`
      : '';
    const proposalCards = proposals.map(p => _repairChatRenderProposal(p)).join('');
    return `<div style="align-self:flex-start;max-width:88%;background:var(--bg1);border:1px solid var(--border);border-radius:10px 10px 10px 2px;padding:8px 12px">
      <div style="font-size:0.68rem;color:var(--text3);margin-bottom:3px">${escHtml(_repairChatBotId || 'bot')} · ${escHtml(ts)}${entry.cost_usd ? ` · $${(+entry.cost_usd).toFixed(4)}` : ''}</div>
      <div style="font-size:0.85rem;white-space:pre-wrap">${escHtml(entry.text || '')}</div>
      ${proposalCards}
      ${errBlock}
    </div>`;
  }
  if (role === 'apply') {
    const action = entry.action || 'apply';
    const result = entry.result || {};
    const summary = result.field || result.path || result.signature || action;
    return `<div style="align-self:center;font-size:0.7rem;color:var(--text3);font-style:italic">↳ Applied <code>${escHtml(action)}</code>${summary && summary !== action ? ` on <code>${escHtml(String(summary))}</code>` : ''} · ${escHtml(ts)}</div>`;
  }
  return '';
}

function _repairChatRenderProposal(p) {
  if (!p || !p.id) return '';
  const action = p.action || '';
  const payload = p.payload || {};
  const applied = _repairChatAppliedIds.has(p.id);

  // Per-action preview body.
  let preview = '';
  if (action === 'propose_field_edit') {
    const after = payload.after === undefined ? '(unset)' : JSON.stringify(payload.after);
    preview = `<div style="font-size:0.74rem"><strong>Field:</strong> <code>${escHtml(payload.field || '?')}</code></div>
      <div style="font-size:0.74rem;margin-top:3px"><strong>After:</strong> <code style="white-space:pre-wrap;word-break:break-word">${escHtml(after.length > 240 ? after.slice(0, 240) + '…' : after)}</code></div>
      ${payload.rationale ? `<div style="font-size:0.72rem;color:var(--text2);margin-top:3px">${escHtml(payload.rationale)}</div>` : ''}`;
  } else if (action === 'propose_file_edit') {
    preview = `<div style="font-size:0.74rem"><strong>Path:</strong> <code>${escHtml(payload.path || '?')}</code></div>
      <div style="font-size:0.74rem;margin-top:3px"><strong>Summary:</strong> ${escHtml(payload.summary || '')}</div>
      <div style="font-size:0.7rem;color:var(--text3);margin-top:3px">Applying queues this as a known_issue on the manifest. The admin server never auto-edits code under /Users/&lt;bot&gt;/.</div>`;
  } else if (action === 'propose_test_exemption') {
    preview = `<div style="font-size:0.74rem"><strong>Reason:</strong> ${escHtml(payload.reason || '')}</div>`;
  } else if (action === 'mark_resolved') {
    preview = `<div style="font-size:0.74rem"><strong>Signature:</strong> <code>${escHtml((payload.signature || '').slice(0, 16))}</code></div>
      ${payload.rationale ? `<div style="font-size:0.72rem;color:var(--text2);margin-top:3px">${escHtml(payload.rationale)}</div>` : ''}`;
  } else if (action === 'done') {
    preview = `<div style="font-size:0.74rem;color:var(--text2)">${escHtml(_repairChatBotId || 'The bot')} signals the conversation is complete.</div>`;
  } else {
    preview = `<div style="font-size:0.74rem"><code>${escHtml(JSON.stringify(payload).slice(0, 240))}</code></div>`;
  }

  const actionLabel = ({
    propose_field_edit: 'Field edit',
    propose_file_edit:  'File edit (queue)',
    propose_test_exemption: 'Test exemption',
    mark_resolved:      'Mark resolved',
    done:               'Done',
  })[action] || action;
  const buttons = applied
    ? `<button class="btn btn-sm" disabled style="font-size:0.72rem">✓ Applied</button>`
    : `<button class="btn btn-primary btn-sm" style="font-size:0.72rem" onclick="repairChatApply('${escHtml(p.id)}', '${escHtml(action)}')">Apply</button>
       <button class="btn btn-ghost btn-sm" style="font-size:0.72rem" onclick="repairChatSkip('${escHtml(p.id)}')">Skip</button>`;
  return `<div data-proposal-id="${escHtml(p.id)}" style="margin-top:8px;padding:8px 10px;background:var(--bg2);border:1px solid var(--border);border-radius:6px">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:5px">
      <div style="font-size:0.72rem;font-weight:600;color:var(--text2)">${escHtml(actionLabel)}</div>
      <div style="display:flex;gap:5px">${buttons}</div>
    </div>
    ${preview}
  </div>`;
}

async function repairChatSend() {
  if (_repairChatBusy) return;
  const input = document.getElementById('repair-chat-input');
  const send = document.getElementById('repair-chat-send');
  const message = (input && input.value || '').trim();
  if (!message) { toast('Type a message first', 'warn'); return; }
  _repairChatBusy = true;
  if (input) input.disabled = true;
  if (send) { send.disabled = true; send.textContent = 'Thinking…'; }
  try {
    const r = await api('POST', `/api/applications/${_repairChatBotId}/${_repairChatAppId}/repair-chat/message`, {message});
    if (!r || !r.ok) {
      if (r && r.error) toast('✗ ' + r.error, 'err');
      else toast('✗ Send failed', 'err');
      return;
    }
    // Clear the input on success so the next turn types fresh.
    if (input) input.value = '';
    // Reload full state so usage / lock / log all refresh.
    const state = await api('GET', `/api/applications/${_repairChatBotId}/${_repairChatAppId}/repair-chat/state`);
    if (state && state.ok) _repairChatRenderState(state);
  } catch (e) {
    toast('✗ ' + (e && e.message || 'send failed'), 'err');
  } finally {
    _repairChatBusy = false;
    if (input) input.disabled = false;
    if (send) { send.disabled = false; send.textContent = 'Send'; }
    setTimeout(() => { const t = document.getElementById('repair-chat-input'); if (t) t.focus(); }, 10);
  }
}

async function repairChatApply(proposalId, action) {
  if (!proposalId) return;
  // Belt-and-braces confirmation for field/file edits — make sure the
  // operator clicked deliberately. test_exemption / mark_resolved /
  // done are reversible enough not to gate.
  if (action === 'propose_field_edit' || action === 'propose_file_edit') {
    if (!await confirmModal(`Apply this ${action === 'propose_field_edit' ? 'manifest field edit' : 'file-edit TODO'}? The change saves with provenance via repair_chat.`)) return;
  }
  try {
    const r = await api('POST', `/api/applications/${_repairChatBotId}/${_repairChatAppId}/repair-chat/apply`, {proposal_id: proposalId});
    if (!r || !r.ok) {
      toast('✗ ' + ((r && r.error) || 'apply failed'), 'err');
      return;
    }
    toast('✓ Applied', 'ok');
    _repairChatAppliedIds.add(proposalId);
    // Refresh state so the card shows the applied stamp + the apply row
    // appears in the log.
    const state = await api('GET', `/api/applications/${_repairChatBotId}/${_repairChatAppId}/repair-chat/state`);
    if (state && state.ok) _repairChatRenderState(state);
    // Done proposal releases the lock + we should refresh the outer
    // Apps modal so the operator sees the bot's view of the new state.
    if (action === 'done') {
      const modal = document.getElementById('repair-chat-modal');
      if (modal) modal.remove();
      if (typeof _refreshCoherenceModal === 'function') await _refreshCoherenceModal();
    }
  } catch (e) {
    toast('✗ ' + (e && e.message || 'apply failed'), 'err');
  }
}

function repairChatSkip(proposalId) {
  // Skip is a UI-only dismissal — the proposal stays in the log for
  // audit but the card disables. We mark it locally so the operator
  // can tell at a glance.
  const card = document.querySelector(`[data-proposal-id="${proposalId}"]`);
  if (!card) return;
  card.style.opacity = '0.4';
  const btns = card.querySelectorAll('button');
  btns.forEach(b => { b.disabled = true; });
  const skipHint = document.createElement('div');
  skipHint.style.cssText = 'font-size:0.7rem;color:var(--text3);margin-top:4px;font-style:italic';
  skipHint.textContent = '(skipped)';
  card.appendChild(skipHint);
}

async function repairChatClose() {
  // Release the lock so the next operator can pick up immediately.
  // Best-effort — if the call fails the TTL will release the lock
  // within 30 min.
  try {
    if (_repairChatBotId && _repairChatAppId) {
      await api('POST', `/api/applications/${_repairChatBotId}/${_repairChatAppId}/repair-chat/end`);
    }
  } catch (_) { /* swallow */ }
  const modal = document.getElementById('repair-chat-modal');
  if (modal) modal.remove();
  _repairChatBotId = null;
  _repairChatAppId = null;
  // Refresh the outer manifest modal so any newly-applied changes show.
  if (typeof _refreshCoherenceModal === 'function') {
    try { await _refreshCoherenceModal(); } catch (_) { /* swallow */ }
  }
}

async function _fetchFileIndex(botId, pkgId, manifestFiles) {
  // Capture modal identity at call time — guard against stale updates if user
  // navigates to a different app while the fetch is in flight.
  const snapBotId = _mBotId;
  const snapAppId = _mAppId;
  try {
    const r = await fetch(`/api/bots/${encodeURIComponent(botId)}/file-index?pkg_id=${encodeURIComponent(pkgId)}`);
    if (!r.ok) return;
    const data = await r.json();
    const index = data.index || {};

    // Build a map of file_ids already in the manifest
    const manifestFileIds = new Set(
      manifestFiles.filter(f => f && typeof f === 'object' && f.file_id).map(f => f.file_id)
    );

    // Unregistered: in index but not in manifest
    const unregistered = Object.entries(index)
      .filter(([fid]) => !manifestFileIds.has(fid))
      .map(([fid, rec]) => ({ file_id: fid, ...rec }));

    // Guard: bail if the modal has changed to a different app since we started
    if (_mBotId !== snapBotId || _mAppId !== snapAppId) return;
    const body = document.getElementById('manifest-files-body');
    if (!body) return;

    // Rebuild with live index data
    const _layerBadgeCls = {
      script: 'forge', skill: 'agent', policy: 'security_bot', orchestrator: 'autonomous',
      test: 'inline', reference: 'member', data: 'ok', state: 'warn'
    };
    const _lcDotColor = {
      owned: '#4ade80', shared: '#7eb8f7',
      // "external" replaces the old "orphaned" label; amber, not red
      // — owned by a different app's manifest, not broken.
      external: '#f59e0b',
      orphaned: '#f59e0b',  // backward-compat in case index returns legacy key
      unowned: 'var(--text3)'
    };
    const badge = (text, cls) => `<span class="badge badge-${cls}">${escHtml(text)}</span>`;
    const secStyle = 'border:1px solid var(--border);border-radius:8px;padding:14px 16px;margin-bottom:12px;background:var(--bg2)';

    const th = txt => `<th style="text-align:left;padding:3px 8px 6px 0;font-size:0.67rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3)">${txt}</th>`;
    const thm = txt => `<th style="text-align:left;padding:3px 8px 6px;font-size:0.67rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3)">${txt}</th>`;

    const rawFiles = Array.isArray(_mData.files) ? _mData.files : [];
    const rawCrons = Array.isArray(_mData.crons) ? _mData.crons : [];
    const m = _mData;

    const fRows = rawFiles.map(f => {
      const isStr = typeof f === 'string';
      const fpath   = isStr ? f : (f.path || '');
      const fileId  = isStr ? '' : (f.file_id || '');
      const idxRec  = fileId ? index[fileId] : null;
      const layer   = (idxRec && idxRec.layer) || (!isStr && f.layer) || '';
      const purpose = isStr ? '' : (f.purpose || '');
      const ownedBy = isStr ? '' : (f.owned_by || '');
      const sw      = isStr ? [] : (f.shared_with || []);
      let lc2 = idxRec ? idxRec.lifecycle : 'unowned';
      if (!idxRec) {
        if (ownedBy === m.pkg_id) lc2 = sw.length > 0 ? 'shared' : 'owned';
        else if (ownedBy) lc2 = 'external'; // owned by a different app
        else if (fileId) lc2 = 'owned';
      }
      // Backward compat: normalize legacy "orphaned" → "external" for display.
      if (lc2 === 'orphaned') lc2 = 'external';
      const dotColor = _lcDotColor[lc2] || 'var(--text3)';
      const fileName = fpath.split('/').pop() || fpath;
      const lcTitle = lc2 === 'external'
        ? `Owned by another app's manifest (pkg_id ${ownedBy || '?'}). Not orphaned — just attributed elsewhere.`
        : '';
      return `<tr style="border-bottom:1px solid var(--border)">
        <td style="padding:5px 8px 5px 0;vertical-align:top">
          <div style="font-family:monospace;font-size:0.72rem;color:var(--text1)">${escHtml(fileName)}</div>
          <div style="font-size:0.65rem;color:var(--text3);word-break:break-all">${escHtml(fpath)}</div>
        </td>
        <td style="padding:5px 8px;vertical-align:top;white-space:nowrap">${layer ? badge(layer, _layerBadgeCls[layer] || 'member') : '<span style="color:var(--text3)">—</span>'}</td>
        <td style="padding:5px 8px;vertical-align:top;white-space:nowrap"${lcTitle ? ` title="${escHtml(lcTitle)}"` : ''}><span style="display:inline-flex;align-items:center;gap:4px"><span style="width:7px;height:7px;border-radius:50%;background:${dotColor};flex-shrink:0"></span><span style="font-size:0.72rem;color:var(--text2)">${lc2}</span></span></td>
        <td style="padding:5px 8px;vertical-align:top;font-family:monospace;font-size:0.7rem;color:var(--text3);white-space:nowrap">${escHtml(fileId || '—')}</td>
        <td style="padding:5px 0 5px 8px;vertical-align:top;font-size:0.72rem;color:var(--text2)">${escHtml(purpose || '—')}</td>
      </tr>`;
    }).join('');

    const unregRows = unregistered.map(r => {
      const fileName = (r.path || '').split('/').pop() || r.path;
      const dotColor = _lcDotColor[r.lifecycle] || 'var(--text3)';
      return `<tr style="border-bottom:1px solid var(--border)">
        <td style="padding:5px 8px 5px 0;vertical-align:top">
          <div style="font-family:monospace;font-size:0.72rem;color:var(--text1)">${escHtml(fileName)}</div>
          <div style="font-size:0.65rem;color:var(--text3);word-break:break-all">${escHtml(r.path || '')}</div>
        </td>
        <td style="padding:5px 8px;vertical-align:top;white-space:nowrap">${r.layer ? badge(r.layer, _layerBadgeCls[r.layer] || 'member') : '<span style="color:var(--text3)">—</span>'}</td>
        <td style="padding:5px 8px;vertical-align:top;white-space:nowrap"><span style="display:inline-flex;align-items:center;gap:4px"><span style="width:7px;height:7px;border-radius:50%;background:${dotColor};flex-shrink:0"></span><span style="font-size:0.72rem;color:var(--text2)">${escHtml(r.lifecycle || 'unowned')}</span></span></td>
        <td style="padding:5px 8px;vertical-align:top;font-family:monospace;font-size:0.7rem;color:var(--text3);white-space:nowrap">${escHtml(r.file_id || '—')}</td>
        <td style="padding:5px 0 5px 8px;vertical-align:top;font-size:0.72rem;color:var(--yellow)">not in manifest</td>
      </tr>`;
    }).join('');

    const hasFiles = rawFiles.length > 0;
    const hasCrons = rawCrons.length > 0;
    const hasUnreg = unregRows.length > 0;

    const cronRows = rawCrons.map(c => {
      const isStr = typeof c === 'string';
      const schedule = isStr ? c : (c.schedule || c.cron || '');
      const script   = isStr ? '' : (c.script || '');
      const label    = isStr ? '' : (c.label || c.description || '');
      const cronFileId = isStr ? '' : (c.file_id || '');
      return `<tr style="border-bottom:1px solid var(--border)">
        <td style="padding:5px 8px 5px 0;font-family:monospace;font-size:0.72rem;color:var(--text1);white-space:nowrap">${escHtml(schedule)}</td>
        <td style="padding:5px 8px;font-family:monospace;font-size:0.72rem;color:var(--text2)">${escHtml(script || '—')}</td>
        <td style="padding:5px 8px;font-size:0.72rem;color:var(--text2)">${escHtml(label || '—')}</td>
        <td style="padding:5px 0;font-family:monospace;font-size:0.7rem;color:var(--text3)">${escHtml(cronFileId || '—')}</td>
      </tr>`;
    }).join('');

    body.innerHTML = `
      ${hasFiles || hasUnreg ? `<div style="overflow-x:auto${hasCrons ? ';margin-bottom:16px' : ''}">
        <table style="width:100%;border-collapse:collapse">
          <thead><tr style="border-bottom:1px solid var(--border)">
            ${th('Path')}${thm('Layer')}${thm('Lifecycle')}${thm('File ID')}<th style="text-align:left;padding:3px 0 6px 8px;font-size:0.67rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3)">Purpose</th>
          </tr></thead>
          <tbody>${fRows}${unregRows}</tbody>
        </table>
      </div>` : `<div style="font-size:0.78rem;color:var(--text3);margin-bottom:${hasCrons ? '14px' : '0'}">No files registered. Run a workspace scan to discover and link component files.</div>`}
      ${hasCrons ? `<div style="font-size:0.72rem;font-weight:700;color:var(--text2);text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px">Cron jobs</div>
      <div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse">
        <thead><tr style="border-bottom:1px solid var(--border)">
          ${th('Schedule')}${thm('Script')}${thm('Label')}<th style="text-align:left;padding:3px 0 6px 8px;font-size:0.67rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3)">File ID</th>
        </tr></thead>
        <tbody>${cronRows}</tbody>
      </table></div>` : ''}`;
  } catch (_) { /* best-effort — leave initial render in place */ }
}

function _setStar(n) {
  document.getElementById('m-sat-score').value = n;
  document.getElementById('m-stars').innerHTML = [1,2,3,4,5].map(i =>
    `<span class="star" style="font-size:1.4rem;cursor:pointer;color:${i<=n?'var(--yellow)':'var(--text3)'}" onclick="_setStar(${i})" title="${i} star${i>1?'s':''}">${i<=n?'★':'☆'}</span>`
  ).join('');
}

function editManifest() { _mEditMode = true; _renderManifestModal(); }
function _cancelEdit() { _mEditMode = false; _renderManifestModal(); }

function _linesOf(id) {
  const el = document.getElementById(id);
  if (!el) return [];
  return el.value.split('\n').map(s=>s.trim()).filter(Boolean);
}

// addTc / removeTc / runTestCase removed 2026-06-08 — app-test surface killed
// per docs/decision-app-tests-2026-06-08.md.

// ── App audit on-demand (Tier 3) ─────────────────────────────────────────
async function _runAuditNow(fullAudit) {
  if (!_mBotId || !_mAppId) return;
  try {
    const r = await api(
      'POST',
      `/api/applications/${_mBotId}/${_mAppId}/audit`,
      { full_audit: !!fullAudit },
    );
    if (r.ok) {
      toast(`✓ Queued audit ${r.request_id}${r.kicked ? ' (kicked)' : ' (queued)'}`, 'ok');
    } else {
      toast('✗ Audit dispatch failed: ' + (r.error || '?'), 'err');
    }
  } catch (e) {
    toast('✗ Audit failed: ' + e, 'err');
  }
}

async function _viewAuditTrail() {
  if (!_mBotId || !_mAppId) return;
  const esc = escHtml;
  try {
    const r = await api(
      'GET',
      `/api/applications/${_mBotId}/${_mAppId}/audit/trail?limit=50`,
    );
    const entries = (r && r.entries) || [];
    let html;
    if (!entries.length) {
      html = `<div style="padding:24px;color:var(--text3);text-align:center">No audit trail yet for ${esc(_mAppId)}.</div>`;
    } else {
      html = '<table style="width:100%;border-collapse:collapse;font-size:0.8rem"><thead><tr style="text-align:left;color:var(--text2);border-bottom:1px solid var(--border)"><th style="padding:4px 8px">When</th><th style="padding:4px 8px">Kind</th><th style="padding:4px 8px">Detail</th></tr></thead><tbody>';
      for (const e of entries.slice().reverse()) {
        const ts = esc(e.ts || '?');
        const kind = esc(e.kind || '?');
        let detail = '';
        if (e.kind === 'audit_run') {
          detail = `tier${e.tier || '?'} · ${esc(e.status || '')} · ${e.findings_count || 0} findings`;
        } else if (e.kind && e.kind.startsWith('tier')) {
          detail = `${esc(e.severity || '')} · ${esc((e.summary || e.rationale || '').slice(0, 100))}`;
        } else {
          detail = esc(JSON.stringify(e).slice(0, 200));
        }
        html += `<tr style="border-bottom:1px solid var(--border3)"><td style="padding:4px 8px;color:var(--text3);font-family:monospace">${ts}</td><td style="padding:4px 8px">${kind}</td><td style="padding:4px 8px">${detail}</td></tr>`;
      }
      html += '</tbody></table>';
    }
    const wrap = `<div style="padding:16px"><h3 style="margin:0 0 12px 0;font-size:1rem">Audit trail — ${esc(_mAppId)}</h3>${html}</div>`;
    if (typeof openCustomModal === 'function') {
      openCustomModal(wrap);
    } else {
      // Fallback: write into a generic modal slot if available, otherwise alert with summary
      const target = document.getElementById('m-audit-trail-modal');
      if (target) {
        target.innerHTML = wrap;
        target.style.display = 'block';
      } else {
        const win = window.open('', '_blank');
        if (win) {
          win.document.write('<title>Audit trail</title>' + wrap);
        } else {
          toast('Trail loaded (' + entries.length + ' entries) — popups blocked', 'warn');
        }
      }
    }
  } catch (e) {
    toast('✗ Trail load failed: ' + e, 'err');
  }
}

async function _unacceptFinding(signature) {
  if (!_mBotId || !_mAppId || !signature) return;
  if (!await confirmModal({body: 'Un-accept this finding? Future audits may re-raise it.', danger: true})) return;
  try {
    const r = await api(
      'POST',
      `/api/applications/${_mBotId}/${_mAppId}/audit/unaccept`,
      { signature },
    );
    if (r.ok) {
      toast('✓ Un-accepted', 'ok');
      // Patch local state and re-render
      _mData.audit_accepted = (_mData.audit_accepted || []).filter(
        a => a && a.signature !== signature,
      );
      _renderManifestModal();
    } else {
      toast('✗ Un-accept failed: ' + (r.error || '?'), 'err');
    }
  } catch (e) {
    toast('✗ Un-accept failed: ' + e, 'err');
  }
}

async function saveManifest() {
  // Collect identity
  const identity = {
    purpose: document.getElementById('m-purpose')?.value.trim() || '',
    scope_includes: _linesOf('m-scope-in'),
    scope_excludes: _linesOf('m-scope-out'),
    user: document.getElementById('m-user')?.value.trim() || '',
  };
  // Collect success_criteria
  const success_criteria = {
    observable_outcomes: _linesOf('m-outcomes'),
    failure_signals: _linesOf('m-failure'),
    quality_bar: {
      minimum: document.getElementById('m-qb-min')?.value.trim() || '',
      excellent: document.getElementById('m-qb-exc')?.value.trim() || '',
    },
  };
  // Collect constraints
  const constraints = {
    privacy: _linesOf('m-privacy'),
    safety: _linesOf('m-safety'),
    dependencies: _linesOf('m-deps'),
    boundaries: _linesOf('m-bounds'),
  };
  // test_cases editor removed 2026-06-08; preserve any existing on-disk
  // test_cases verbatim so saving the manifest doesn't drop them.
  const test_cases = _mData.test_cases || [];
  // Collect satisfaction
  const satScore = document.getElementById('m-sat-score')?.value;
  const satNotes = document.getElementById('m-sat-notes')?.value.trim() || '';
  const satisfaction = {
    score: satScore ? parseInt(satScore, 10) : null,
    notes: satNotes || null,
    rated_at: satScore ? new Date().toISOString() : (_mData.satisfaction?.rated_at || null),
  };
  // Collect example triggers
  const example_triggers = _linesOf('m-triggers');
  // Collect known issues
  const known_issues = _linesOf('m-issues');

  // Testing-config editors removed 2026-06-08; preserve any existing
  // on-disk values verbatim.
  const test_cadence = _mData.test_cadence ?? null;
  const test_exemption_reason = _mData.test_exemption_reason || '';

  // Collect audit-config fields (manifest v12)
  const auditCadenceVal = document.getElementById('m-audit-cadence')?.value;
  const audit_cadence = auditCadenceVal ? auditCadenceVal : null;
  const auditEligibleEl = document.getElementById('m-audit-eligible');
  const audit_eligible = auditEligibleEl ? !!auditEligibleEl.checked : _mData.audit_eligible;

  const body = {
    ..._mData,
    identity, success_criteria, constraints, test_cases, satisfaction,
    example_triggers, known_issues,
    test_cadence, test_exemption_reason,
    audit_cadence, audit_eligible,
    // Keep legacy flat fields in sync
    satisfaction_score: satisfaction.score,
    satisfaction_notes: satisfaction.notes,
  };

  const r = await api('PUT', `/api/applications/${_mBotId}/${_mAppId}`, body);
  if (r.ok) {
    toast('✓ Manifest saved', 'ok');
    _mData = body;
    _mEditMode = false;
    _renderManifestModal();
    await loadCapabilities();
  } else {
    toast('✗ ' + (r.error || 'Save failed'), 'err');
  }
}

// Lifecycle action handlers (_lcAction / _lcViewSpec) removed 2026-05-26 —
// see _renderManifestModal's "Lifecycle section: REMOVED" comment. Forge-job
// progression state belongs on the Forge Jobs tab, not on installed apps;
// nothing in this file calls these anymore. The PATCH-by-step write path
// went with them.

function closeManifestModal() {
  document.getElementById('manifest-modal').classList.remove('open');
  _mBotId = null; _mAppId = null; _mData = null; _mEditMode = false;
}

// ── App deletion ──────────────────────────────────────
// Two-step flow:
//  Step 1: fetch the breakdown (preserved, cleaned, candidates) — show confirm dialog
//  Step 2: user selects which candidates to delete → DELETE with delete_files list

let _deleteBreakdown = null;

async function _confirmDeleteApp(botId, appId, appName) {
  // Step 1: dry-run — get breakdown without deleting anything yet
  const r = await api('DELETE', `/api/applications/${botId}/${appId}`, {delete_files: []});
  if (!r || r.error) {
    toast('Could not start deletion: ' + (r?.error || 'unknown error'), 'err');
    return;
  }
  _deleteBreakdown = {botId, appId, appName, ...r};
  _renderDeleteModal();
}

function _renderDeleteModal() {
  const d = _deleteBreakdown;
  if (!d) return;

  const preserve = d.preserved_files || [];
  const cleaned  = d.cleaned_files || [];
  const cands    = d.deletion_candidates || [];

  const preserveHtml = preserve.length
    ? `<div style="margin-bottom:10px">
        <div style="font-size:0.8rem;font-weight:600;color:var(--yellow);margin-bottom:4px">⚠️ Data files — always preserved (${preserve.length})</div>
        ${preserve.map(p => `<div style="font-size:0.75rem;color:var(--text2);padding:2px 0">${escHtml(p)}</div>`).join('')}
       </div>` : '';

  const cleanedHtml = cleaned.length
    ? `<div style="margin-bottom:10px">
        <div style="font-size:0.8rem;font-weight:600;color:var(--blue);margin-bottom:4px">🔗 Shared files — marker updated only (${cleaned.length})</div>
        ${cleaned.map(p => `<div style="font-size:0.75rem;color:var(--text2);padding:2px 0">${escHtml(p)}</div>`).join('')}
       </div>` : '';

  const candsHtml = cands.length
    ? `<div style="margin-bottom:10px">
        <div style="font-size:0.8rem;font-weight:600;color:var(--red);margin-bottom:4px">🗑 Unowned after deletion — select files to delete (${cands.length})</div>
        ${cands.map((p,i) => `<label style="display:flex;align-items:center;gap:6px;font-size:0.75rem;color:var(--text1);padding:2px 0;cursor:pointer">
          <input type="checkbox" id="del-cand-${i}" checked style="accent-color:var(--red)"> ${escHtml(p)}
        </label>`).join('')}
       </div>` : '<div style="font-size:0.8rem;color:var(--text2)">No unowned logic files to clean up.</div>';

  const modal = document.getElementById('delete-app-modal');
  document.getElementById('delete-app-modal-body').innerHTML = `
    <div style="font-size:0.95rem;font-weight:700;margin-bottom:12px">Delete <em>${escHtml(d.appName)}</em>?</div>
    <div style="font-size:0.82rem;color:var(--text2);margin-bottom:14px">This will remove the manifest and clean up its component files as described below. This cannot be undone.</div>
    ${preserveHtml}${cleanedHtml}${candsHtml}`;
  modal.classList.add('open');
}

async function _executeDeleteApp() {
  const d = _deleteBreakdown;
  if (!d) return;
  const cands = d.deletion_candidates || [];
  const toDelete = cands.filter((_,i) => document.getElementById(`del-cand-${i}`)?.checked);
  _closeDeleteModal();
  closeManifestModal();
  const r = await api('DELETE', `/api/applications/${d.botId}/${d.appId}`, {delete_files: toDelete});
  if (!r || r.error) {
    toast('Deletion failed: ' + (r?.error || 'unknown error'), 'err');
    return;
  }
  loadApplications();
  const summary = [
    r.actually_deleted?.length ? `${r.actually_deleted.length} file(s) deleted` : '',
    r.preserved_files?.length ? `${r.preserved_files.length} data file(s) preserved` : '',
    r.cleaned_files?.length ? `${r.cleaned_files.length} shared file(s) updated` : '',
  ].filter(Boolean).join(', ');
  _showToast(`App removed. ${summary}`, 'success');
  const manual = r.manual_delete_required || [];
  if (manual.length) {
    toast(`${manual.length} file(s) need manual deletion (recorded in the archived manifest): ${manual.map(m => m.path).join(', ')}`, 'warn');
  }
}

function _closeDeleteModal() {
  document.getElementById('delete-app-modal')?.classList.remove('open');
  _deleteBreakdown = null;
}

// ═══════════════════════════════════════════════════════════════════════════
// APP LIFECYCLE — PAUSE / ARCHIVE / RESTORE + UNINSTALL WIZARD
// ═══════════════════════════════════════════════════════════════════════════

// ── Quick single-action helpers ───────────────────────────────────────────────

function _capName(appId) {
  // Look up display name from cached cap list — avoids embedding names in onclick attrs
  return (_capData || []).find(c => c.id === appId)?.name || appId;
}

async function pauseApp(botId, appId) {
  const appName = _capName(appId);
  if (!await confirmModal({body: `Pause "${appName}"?\n\nThis will disable its cron jobs. Nothing is deleted. You can unpause at any time.`, danger: true})) return;
  const r = await api('POST', `/api/applications/${botId}/${appId}/pause`, {});
  if (r?.ok) {
    toast(`⏸ "${appName}" paused`, 'ok');
    await loadCapabilities();
  } else {
    toast(r?.error || 'Pause failed', 'err');
  }
}

async function unpauseApp(botId, appId) {
  const appName = _capName(appId);
  const r = await api('POST', `/api/applications/${botId}/${appId}/unpause`, {});
  if (r?.ok) {
    const cronNote = r.crons?.enabled > 0 ? ` (${r.crons.enabled} cron(s) re-enabled)` : '';
    toast(`▶ "${appName}" resumed${cronNote}`, 'ok');
    await loadCapabilities();
  } else {
    toast(r?.error || 'Unpause failed', 'err');
  }
}

async function archiveApp(botId, appId) {
  const appName = _capName(appId);
  if (!await confirmModal({body: `Archive "${appName}"?\n\nThis will disable its cron jobs and hide it from the Applications view. Nothing is deleted. You can restore it at any time.`, danger: true})) return;
  const r = await api('POST', `/api/applications/${botId}/${appId}/archive`, {});
  if (r?.ok) {
    toast(`📦 "${appName}" archived`, 'ok');
    await loadCapabilities();
  } else {
    toast(r?.error || 'Archive failed', 'err');
  }
}

async function restoreApp(botId, appId) {
  const appName = _capName(appId);
  const r = await api('POST', `/api/applications/${botId}/${appId}/restore`, {});
  if (r?.ok) {
    const cronNote = r.crons?.enabled > 0 ? ` (${r.crons.enabled} cron(s) re-enabled)` : '';
    toast(`✓ "${appName}" restored${cronNote}`, 'ok');
    await loadCapabilities();
  } else {
    toast(r?.error || 'Restore failed', 'err');
  }
}

// ── Uninstall Wizard ──────────────────────────────────────────────────────────

let _uwiz = null;  // { botId, appId, appName, step, scope, dependents, dryRun }

function openUninstallWizard(botId, appId) {
  const appName = _capName(appId);
  _uwiz = { botId, appId, appName, step: 1, scope: 'archive', dependents: null, dryRun: null };
  document.getElementById('uninstall-wizard-modal').classList.add('open');
  _uwizRender();
  _uwizStep1Load();
}

function _uwizClose() {
  document.getElementById('uninstall-wizard-modal').classList.remove('open');
  _uwiz = null;
}

function _uwizSetStep(n) {
  if (!_uwiz) return;
  _uwiz.step = n;
  // Update indicator styling
  for (let i = 1; i <= 4; i++) {
    const el = document.getElementById(`uwiz-step-${i}-ind`);
    if (!el) continue;
    el.style.color = i === n ? 'var(--accent)' : (i < n ? 'var(--green)' : 'var(--text3)');
    el.style.fontWeight = i === n ? '600' : '400';
  }
}

function _uwizRender() {
  if (!_uwiz) return;
  _uwizSetStep(_uwiz.step);
  const body = document.getElementById('uwiz-body');
  const nav  = document.getElementById('uwiz-nav');
  switch (_uwiz.step) {
    case 1: _uwizRenderStep1(body, nav); break;
    case 2: _uwizRenderStep2(body, nav); break;
    case 3: _uwizRenderStep3(body, nav); break;
    case 4: /* Step 4 is rendered by _uwizExecute() directly */ break;
  }
}

// Step 1: Check dependents
async function _uwizStep1Load() {
  const body = document.getElementById('uwiz-body');
  body.innerHTML = '<div style="color:var(--text3);font-size:0.85rem;padding:20px 0;text-align:center">Checking for dependent apps…</div>';
  document.getElementById('uwiz-nav').innerHTML = '';
  try {
    const r = await api('GET', `/api/applications/${_uwiz.botId}/${_uwiz.appId}/dependents`);
    _uwiz.dependents = r.dependents || [];
    _uwizRender();
  } catch(e) {
    body.innerHTML = `<div style="color:var(--red);font-size:0.85rem">Failed to check dependents: ${escHtml(e.message)}</div>`;
    document.getElementById('uwiz-nav').innerHTML = `<button class="btn btn-ghost" onclick="_uwizClose()">Cancel</button>`;
  }
}

function _uwizRenderStep1(body, nav) {
  const deps = _uwiz.dependents || [];
  const required = deps.filter(d => d.required);
  const optional = deps.filter(d => !d.required);

  let html = `<div style="font-size:0.95rem;font-weight:700;margin-bottom:8px">Uninstall: ${escHtml(_uwiz.appName)}</div>`;

  if (!deps.length) {
    html += `<div style="padding:10px 12px;background:rgba(61,220,132,0.08);border:1px solid rgba(61,220,132,0.25);border-radius:6px;font-size:0.82rem;color:var(--green);margin-bottom:14px">
      ✓ No other apps depend on this one. Safe to proceed.
    </div>`;
  } else {
    if (required.length) {
      html += `<div style="padding:10px 12px;background:rgba(255,107,107,0.1);border:1px solid rgba(255,107,107,0.3);border-radius:6px;font-size:0.82rem;color:var(--red);margin-bottom:10px">
        ⚠ ${required.length} app(s) require this one — they will break if you uninstall it:
        <ul style="margin:6px 0 0 16px;padding:0">
          ${required.map(d => `<li>${escHtml(d.name)}</li>`).join('')}
        </ul>
      </div>`;
    }
    if (optional.length) {
      html += `<div style="padding:10px 12px;background:rgba(240,180,41,0.1);border:1px solid rgba(240,180,41,0.3);border-radius:6px;font-size:0.82rem;color:var(--yellow);margin-bottom:10px">
        ⚠ ${optional.length} app(s) optionally use this one:
        <ul style="margin:6px 0 0 16px;padding:0">
          ${optional.map(d => `<li>${escHtml(d.name)}</li>`).join('')}
        </ul>
      </div>`;
    }
  }

  body.innerHTML = html;
  nav.innerHTML = `
    <button class="btn btn-ghost" onclick="_uwizClose()">Cancel</button>
    <button class="btn btn-primary" onclick="_uwiz.step=2;_uwizRender()">
      ${required.length ? 'Proceed anyway →' : 'Next →'}
    </button>`;
}

// Step 2: Choose scope
function _uwizRenderStep2(body, nav) {
  body.innerHTML = `
    <div style="font-size:0.95rem;font-weight:700;margin-bottom:12px">What do you want to do?</div>
    <div style="display:flex;flex-direction:column;gap:10px">
      <label style="display:flex;align-items:flex-start;gap:10px;padding:10px 12px;border:1px solid var(--border);border-radius:8px;cursor:pointer;transition:border-color 0.15s" id="uwiz-opt-archive">
        <input type="radio" name="uwiz-scope" value="archive" ${_uwiz.scope==='archive'?'checked':''} style="width:auto;margin-top:2px" onchange="_uwiz.scope=this.value;_uwizHighlightScope()">
        <div>
          <div style="font-weight:600;font-size:0.85rem">📦 Archive <span style="font-weight:400;color:var(--text3)">(recommended)</span></div>
          <div style="font-size:0.78rem;color:var(--text2);margin-top:2px">Disable crons, hide from view. All files kept. Fully reversible.</div>
        </div>
      </label>
      <label style="display:flex;align-items:flex-start;gap:10px;padding:10px 12px;border:1px solid var(--border);border-radius:8px;cursor:pointer" id="uwiz-opt-keep-data">
        <input type="radio" name="uwiz-scope" value="keep-data" ${_uwiz.scope==='keep-data'?'checked':''} style="width:auto;margin-top:2px" onchange="_uwiz.scope=this.value;_uwizHighlightScope()">
        <div>
          <div style="font-weight:600;font-size:0.85rem">💤 Remove scripts, keep data</div>
          <div style="font-size:0.78rem;color:var(--text2);margin-top:2px">Delete script files, disable crons. Data files preserved. Manifest kept as provenance (status: dormant). Not easily reversible.</div>
        </div>
      </label>
      <label style="display:flex;align-items:flex-start;gap:10px;padding:10px 12px;border:1px solid var(--border);border-radius:8px;cursor:pointer" id="uwiz-opt-full">
        <input type="radio" name="uwiz-scope" value="full" ${_uwiz.scope==='full'?'checked':''} style="width:auto;margin-top:2px" onchange="_uwiz.scope=this.value;_uwizHighlightScope()">
        <div>
          <div style="font-weight:600;font-size:0.85rem;color:var(--red)">🗑 Full uninstall</div>
          <div style="font-size:0.78rem;color:var(--text2);margin-top:2px">Remove scripts, data files, and manifest. Irreversible.</div>
        </div>
      </label>
    </div>`;
  _uwizHighlightScope();
  nav.innerHTML = `
    <button class="btn btn-ghost" onclick="_uwiz.step=1;_uwizRender()">← Back</button>
    <button class="btn btn-ghost" onclick="_uwizClose()">Cancel</button>
    <button class="btn btn-primary" onclick="_uwizStep3Load()">Next →</button>`;
}

function _uwizHighlightScope() {
  ['archive','keep-data','full'].forEach(v => {
    const el = document.getElementById(`uwiz-opt-${v}`);
    if (el) el.style.borderColor = (_uwiz.scope === v) ? 'var(--accent)' : 'var(--border)';
  });
}

// Step 3: Review
async function _uwizStep3Load() {
  _uwiz.step = 3;
  _uwizSetStep(3);
  const body = document.getElementById('uwiz-body');
  const nav  = document.getElementById('uwiz-nav');

  if (_uwiz.scope === 'archive') {
    body.innerHTML = `
      <div style="font-size:0.95rem;font-weight:700;margin-bottom:10px">Review: Archive</div>
      <div style="font-size:0.85rem;color:var(--text2);line-height:1.6">
        <div style="margin-bottom:6px">✓ Cron jobs will be disabled</div>
        <div style="margin-bottom:6px">✓ All files kept intact on disk</div>
        <div style="margin-bottom:6px">✓ Manifest status → <strong>hidden</strong></div>
        <div style="color:var(--green)">✓ Fully reversible — click Restore any time</div>
      </div>`;
    nav.innerHTML = `
      <button class="btn btn-ghost" onclick="_uwiz.step=2;_uwizRender()">← Back</button>
      <button class="btn btn-ghost" onclick="_uwizClose()">Cancel</button>
      <button class="btn btn-primary" onclick="_uwizExecute()">Archive app</button>`;
    return;
  }

  // For keep-data and full: fetch dry-run deletion breakdown
  body.innerHTML = '<div style="color:var(--text3);font-size:0.85rem;padding:20px 0;text-align:center">Analysing files…</div>';
  nav.innerHTML = '';
  try {
    const r = await api('DELETE', `/api/applications/${_uwiz.botId}/${_uwiz.appId}`, { delete_files: [] });
    if (r?.error) throw new Error(r.error);
    _uwiz.dryRun = r;
    _uwizRenderStep3(body, nav);
  } catch(e) {
    body.innerHTML = `<div style="color:var(--red);font-size:0.85rem">Analysis failed: ${escHtml(e.message)}</div>`;
    nav.innerHTML = `<button class="btn btn-ghost" onclick="_uwiz.step=2;_uwizRender()">← Back</button>`;
  }
}

function _uwizRenderStep3(body, nav) {
  if (!_uwiz.dryRun) {
    // Archive scope renders step 3 inline in _uwizStep3Load; dryRun is not needed.
    // Guard against accidental re-render from _uwizRender() switch.
    body.innerHTML = '<div style="font-size:0.85rem;color:var(--text3);padding:8px 0">Ready to execute.</div>';
    nav.innerHTML = `
      <button class="btn btn-ghost" onclick="_uwiz.step=2;_uwizRender()">← Back</button>
      <button class="btn btn-ghost" onclick="_uwizClose()">Cancel</button>
      <button class="btn btn-primary" onclick="_uwizExecute()">Archive app</button>`;
    return;
  }
  const r = _uwiz.dryRun;
  const isKeepData = _uwiz.scope === 'keep-data';
  const preserve = r.preserved_files  || [];  // data/state — always kept
  const cleaned  = r.cleaned_files    || [];  // shared — markers only
  const cands    = r.deletion_candidates || []; // sole-owner scripts

  // For keep-data: scripts go to dormant (not deleted), data preserved
  // For full: scripts + data all become deletable
  const willDelete = isKeepData ? cands : [...cands, ...preserve];
  const willKeep   = isKeepData ? preserve : [];

  let html = `<div style="font-size:0.95rem;font-weight:700;margin-bottom:10px">Review: ${isKeepData ? 'Remove scripts, keep data' : 'Full uninstall'}</div>`;

  if (willDelete.length) {
    html += `<div style="margin-bottom:12px">
      <div style="font-size:0.78rem;font-weight:600;color:var(--red);margin-bottom:5px">Will be deleted (${willDelete.length})</div>
      ${willDelete.map(p => `<div style="font-size:0.73rem;color:var(--text2);padding:1px 0;font-family:monospace">${escHtml(p)}</div>`).join('')}
    </div>`;
  }
  if (cleaned.length) {
    html += `<div style="margin-bottom:12px">
      <div style="font-size:0.78rem;font-weight:600;color:var(--blue);margin-bottom:5px">Shared files — ownership marker updated only (${cleaned.length})</div>
      ${cleaned.map(p => `<div style="font-size:0.73rem;color:var(--text2);padding:1px 0;font-family:monospace">${escHtml(p)}</div>`).join('')}
    </div>`;
  }
  if (willKeep.length) {
    html += `<div style="margin-bottom:12px">
      <div style="font-size:0.78rem;font-weight:600;color:var(--green);margin-bottom:5px">Data files — preserved (${willKeep.length})</div>
      ${willKeep.map(p => `<div style="font-size:0.73rem;color:var(--text2);padding:1px 0;font-family:monospace">${escHtml(p)}</div>`).join('')}
    </div>`;
  }
  if (!willDelete.length && !cleaned.length) {
    html += `<div style="font-size:0.82rem;color:var(--text3)">No tracked files found for this app.</div>`;
  }

  // Phase-4.5 artifacts torn down server-side in the same call (audit S4):
  // launchd/systemd units, python-signal wrappers, heartbeat sections.
  const teardown = r.scheduled_teardown || [];
  const tdActive  = teardown.filter(t => t.kind !== 'scheduled_unit' || t.eligible);
  const tdSkipped = teardown.filter(t => t.kind === 'scheduled_unit' && !t.eligible);
  if (tdActive.length) {
    const tdLabel = t => t.kind === 'heartbeat_section'
      ? `${t.file} § ${t.section_anchor} (managed section)`
      : (t.kind === 'wrapper_file' ? `${t.path} (wrapper)` : `${t.label} (scheduled unit)`);
    html += `<div style="margin-bottom:12px">
      <div style="font-size:0.78rem;font-weight:600;color:var(--red);margin-bottom:5px">Scheduled units &amp; instructions — removed (${tdActive.length})</div>
      ${tdActive.map(t => `<div style="font-size:0.73rem;color:var(--text2);padding:1px 0;font-family:monospace">${escHtml(tdLabel(t))}</div>`).join('')}
    </div>`;
  }
  if (tdSkipped.length) {
    html += `<div style="margin-bottom:12px">
      <div style="font-size:0.78rem;font-weight:600;color:var(--yellow);margin-bottom:5px">Left in place — outside this bot's unit namespace (${tdSkipped.length})</div>
      ${tdSkipped.map(t => `<div style="font-size:0.73rem;color:var(--text2);padding:1px 0;font-family:monospace">${escHtml(t.label)}</div>`).join('')}
    </div>`;
  }

  if (isKeepData) {
    html += `<div style="margin-top:10px;padding:8px 10px;background:rgba(240,180,41,0.08);border:1px solid rgba(240,180,41,0.25);border-radius:6px;font-size:0.78rem;color:var(--yellow)">
      Manifest will be kept with status <strong>dormant</strong> to preserve data provenance.
    </div>`;
  } else {
    html += `<div style="margin-top:10px;padding:8px 10px;background:rgba(255,107,107,0.08);border:1px solid rgba(255,107,107,0.25);border-radius:6px;font-size:0.78rem;color:var(--red)">
      ⚠ The manifest and all associated files will be permanently removed.
    </div>`;
  }

  body.innerHTML = html;
  const btnLabel = isKeepData ? 'Remove scripts' : 'Uninstall everything';
  nav.innerHTML = `
    <button class="btn btn-ghost" onclick="_uwiz.step=2;_uwizRender()">← Back</button>
    <button class="btn btn-ghost" onclick="_uwizClose()">Cancel</button>
    <button class="btn btn-danger" onclick="_uwizExecute()">${btnLabel}</button>`;
}

// Step 4: Execute
async function _uwizExecute() {
  _uwiz.step = 4;
  _uwizSetStep(4);
  const body = document.getElementById('uwiz-body');
  const nav  = document.getElementById('uwiz-nav');

  body.innerHTML = '<div style="color:var(--text3);font-size:0.85rem;padding:10px 0">Running…</div>';
  nav.innerHTML = '';

  const { botId, appId, appName, scope, dryRun } = _uwiz;
  const steps = [];

  try {
    if (scope === 'archive') {
      // ── Archive: just status + cron update
      steps.push('Disabling crons…');
      _uwizProgress(body, steps);
      const r = await api('POST', `/api/applications/${botId}/${appId}/archive`, {});
      if (!r?.ok) throw new Error(r?.error || 'archive failed');
      steps.push('✓ App archived');

    } else if (scope === 'keep-data') {
      // ── Keep data: delete script candidates, then set status=dormant
      const cands = dryRun?.deletion_candidates || [];
      steps.push(`Disabling crons…`);
      _uwizProgress(body, steps);
      // Disable crons first via archive route (we'll override status after)
      await api('POST', `/api/applications/${botId}/${appId}/archive`, {});

      steps.push(`Removing scheduled units + ${cands.length} script file(s)…`);
      _uwizProgress(body, steps);
      // keep_manifest: the server skips manifest deletion and flips status
      // to dormant instead, so the preserved data files keep a provenance
      // manifest. (A prior PATCH-after-DELETE 404'd — the DELETE had already
      // deleted the manifest — leaving keep-data as a full uninstall.)
      const delR = await api('DELETE', `/api/applications/${botId}/${appId}`, { delete_files: cands, commit: true, keep_manifest: true });
      if (delR?.error) throw new Error(delR.error);
      steps.push(`✓ Scripts removed, data preserved (status: dormant)`);
      const manualKeep = delR.manual_delete_required || [];
      if (manualKeep.length) {
        steps.push(`⚠ ${manualKeep.length} file(s) need manual deletion: ${manualKeep.map(m => m.path).join(', ')}`);
      }

    } else {
      // ── Full uninstall
      const cands = dryRun?.deletion_candidates || [];
      const preserve = dryRun?.preserved_files || [];
      const allToDelete = [...cands, ...preserve];

      steps.push('Disabling crons…');
      _uwizProgress(body, steps);
      await api('POST', `/api/applications/${botId}/${appId}/archive`, {});

      steps.push(`Removing scheduled units + deleting ${allToDelete.length} file(s)…`);
      _uwizProgress(body, steps);
      const delR = await api('DELETE', `/api/applications/${botId}/${appId}`, { delete_files: allToDelete, commit: true });
      if (delR?.error) throw new Error(delR.error);

      const nDel = (delR.actually_deleted || []).length;
      const nUnits = (delR.teardown_results || []).filter(t => t.status === 'ok').length;
      steps.push(`✓ ${nDel} file(s) + ${nUnits} scheduled artifact(s) removed, manifest deleted`);
      const manual = delR.manual_delete_required || [];
      if (manual.length) {
        steps.push(`⚠ ${manual.length} file(s) need manual deletion (recorded in the archived manifest): ${manual.map(m => m.path).join(', ')}`);
      }
    }

    _uwizProgress(body, steps, true);
    nav.innerHTML = `<button class="btn btn-primary" onclick="_uwizClose();loadCapabilities()">Done</button>`;
    toast(`✓ "${appName}" uninstalled`, 'ok');

  } catch(e) {
    steps.push(`✗ Error: ${e.message}`);
    _uwizProgress(body, steps, false, true);
    nav.innerHTML = `<button class="btn btn-ghost" onclick="_uwizClose()">Close</button>`;
  }
}

function _uwizProgress(body, steps, done=false, error=false) {
  body.innerHTML = steps.map((s, i) => {
    const isLast = i === steps.length - 1;
    const color = error && isLast ? 'var(--red)' : (done && isLast ? 'var(--green)' : 'var(--text2)');
    return `<div style="font-size:0.85rem;color:${color};padding:3px 0">${escHtml(s)}</div>`;
  }).join('');
}

// ── Reflect: v7-arc manifest hygiene scan ─────────────────────────────────────
// Reflect surfaces four v7-arc findings (the fourth absorbs the legacy
// v6 Orphan Scan's missing-files check):
//  - orphan_file:       file has marker but no Instance.realized_files claims it
//  - missing_marker:    file in realized_files but no marker on disk
//  - stale_pkg_marker:  file has v6 pkg= marker; rewrite_markers missed it
//  - missing_disk_file: Instance claims path, file isn't on disk

const _REFLECT_KIND_META = {
  orphan_file:       { icon: '🪦', color: 'var(--yellow)', label: 'Orphan files',
                        help: 'Marker on disk, no Instance claims them.' },
  missing_marker:    { icon: '❌', color: 'var(--red)',    label: 'Missing markers',
                        help: 'Instance claims them, no marker stamped.' },
  stale_pkg_marker:  { icon: '🔧', color: 'var(--yellow)', label: 'Stale pkg= markers',
                        help: 'v6 form, migration\'s rewrite_markers missed them.' },
  missing_disk_file: { icon: '🗑️', color: 'var(--red)',    label: 'Missing files',
                        help: 'Instance claims path, file isn\'t on disk.' },
};

// ── Shared findings renderers ────────────────────────────────────────────────
// Used by Reflect (the demoted "Re-check only" path) AND the new Sync-apps
// path, which feeds the SAME table its drift_findings. Single-source the
// per-finding Fix/Attach actions and the Reconcile-all affordance here so the
// remediation UX is identical no matter which trigger produced the rows.

// Per-kind icon/count strip (four kinds; dimmed when zero).
function _reflectSummaryItems(counts) {
  counts = counts || {};
  return ['orphan_file', 'missing_marker', 'stale_pkg_marker', 'missing_disk_file']
    .map(kind => {
      const meta = _REFLECT_KIND_META[kind];
      const n = counts[kind] || 0;
      const dim = n === 0 ? ';opacity:0.55' : '';
      return `<div style="font-size:0.78rem${dim}">
        <span style="color:${meta.color};font-weight:700">${meta.icon} ${n}</span>
        ${escHtml(meta.label)}
        <span style="color:var(--text3);font-size:0.7rem"> — ${escHtml(meta.help)}</span>
      </div>`;
    }).join('');
}

// Reconcile-all affordance — appears when orphan_file count > 0. Auto-attaches
// each orphan whose primary marker spec_id resolves to exactly one Instance to
// that Instance's realized_files[]. Residual ambiguous/unmatched stays for
// operator review. See applications/manifest_hygiene.py.
function _reflectReconcileBtn(botId, counts) {
  const orphanCount = (counts || {}).orphan_file || 0;
  if (orphanCount <= 0) return '';
  return `<div style="margin-top:6px">
        <button class="btn btn-primary btn-sm" data-bot="${escHtml(botId)}"
          onclick="reconcileOrphanMarkers(this.dataset.bot)"
          style="font-size:0.72rem;padding:3px 10px">
          Reconcile All (${orphanCount})
        </button>
        <span style="font-size:0.68rem;color:var(--text3);margin-left:8px">
          Attach each orphan to the Instance its marker points at.
        </span>
       </div>`;
}

// Grouped findings tables. Auto-fixable kinds get a per-row Fix; orphan_file
// rows get an inline Attach; everything else is operator-manual. Stashes the
// findings by bot so applyReflectFix / attachOrphanToInstance can recover the
// proposed_action. Returns '' when there are no findings.
function _reflectFindingsTables(botId, findings) {
  findings = findings || [];
  if (typeof window._reflectFindingsByBot !== 'object' || window._reflectFindingsByBot === null) {
    window._reflectFindingsByBot = {};
  }
  window._reflectFindingsByBot[botId] = findings;
  if (!findings.length) return '';
  const _REFLECT_FIXABLE = new Set(['stamp_marker', 'rewrite_marker_to_spec']);
  const _REFLECT_RECONCILABLE = new Set(['orphan_file']);
  const byKind = {};
  for (const f of findings) {
    (byKind[f.kind] = byKind[f.kind] || []).push(f);
  }
  return Object.keys(byKind).map(kind => {
    const meta = _REFLECT_KIND_META[kind] || { icon: '•', color: 'var(--text2)', label: kind };
    const fixable = _REFLECT_FIXABLE.has(kind);
    const reconcilable = _REFLECT_RECONCILABLE.has(kind);
    const rows = byKind[kind].map((f) => {
      const idx = findings.indexOf(f);
      let fixCell;
      if (fixable) {
        fixCell = `<td style="padding:5px 6px;text-align:right">
             <button class="btn btn-ghost btn-sm" data-bot="${escHtml(botId)}" data-idx="${idx}" onclick="applyReflectFix(this.dataset.bot, parseInt(this.dataset.idx))" style="font-size:0.7rem;padding:2px 8px">Fix</button>
           </td>`;
      } else if (reconcilable) {
        // Orphan rows offer an inline "Attach" that runs reconcile scoped
        // to this file only. The Reconcile-All header button does the bulk
        // version. Both call the same endpoint.
        fixCell = `<td style="padding:5px 6px;text-align:right">
             <button class="btn btn-ghost btn-sm" data-bot="${escHtml(botId)}" data-idx="${idx}" onclick="attachOrphanToInstance(this.dataset.bot, parseInt(this.dataset.idx))" style="font-size:0.7rem;padding:2px 8px" title="Attach this file to the Instance whose spec_id matches its marker">Attach</button>
           </td>`;
      } else {
        fixCell = `<td style="padding:5px 6px;color:var(--text3);font-size:0.7rem;text-align:right" title="Operator decision — manual action required">manual</td>`;
      }
      return `<tr style="border-top:1px solid var(--border)">
        <td style="padding:5px 6px;color:var(--text1);font-family:monospace;font-size:0.72rem">${escHtml(f.file_path)}</td>
        <td style="padding:5px 6px;color:var(--text2);font-size:0.72rem">
          ${f.spec_id ? `spec=${escHtml(f.spec_id)}` : ''}
          ${f.instance_id ? `<br>instance=${escHtml(f.instance_id)}` : ''}
        </td>
        <td style="padding:5px 6px;color:var(--text3);font-size:0.72rem">${escHtml(f.description || '')}</td>
        ${fixCell}
      </tr>`;
    }).join('');
    return `<div style="margin-bottom:14px">
      <div style="font-size:0.85rem;font-weight:700;margin-bottom:6px;color:${meta.color}">
        ${meta.icon} ${escHtml(meta.label)} (${byKind[kind].length})
      </div>
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="color:var(--text3);font-size:0.65rem;text-transform:uppercase;letter-spacing:.06em">
          <th style="text-align:left;padding:4px 6px">Path</th>
          <th style="text-align:left;padding:4px 6px">Refs</th>
          <th style="text-align:left;padding:4px 6px">Detail</th>
          <th style="text-align:right;padding:4px 6px;width:60px">Action</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  }).join('');
}

// ── Five-bucket reconciliation display ────────────────────────────────────────
// The reflect endpoint additionally carries the materialized recon ledger under
// `data.recon` (recon_ledger.to_dict()). The five buckets are the AUTHORITATIVE
// grouping — they're what lets the UI tell a scrub_candidate (strip the marker)
// apart from an attach_candidate (register the file), a distinction the flat
// orphan_file finding-kind could not express, so the modal used to offer Attach
// on audit-telemetry / AGENTS.md files where Attach is corrupting. When buckets
// are present we render from them; otherwise we fall back to the legacy
// kind-based tables (handles the not-yet-thin-reader endpoint shape defensively).

const _RECON_BUCKET_META = {
  scrub_candidate:  { icon: '🧹', color: 'var(--red)',    label: 'Scrub candidates',
                       help: 'Marker on a path no app may own (telemetry, AGENTS.md, or a dead app). Strip it — the file content is kept.' },
  attach_candidate: { icon: '🔗', color: 'var(--yellow)', label: 'Attach candidates',
                       help: 'Bot authored them but never registered them. Attach to the app the marker points at.' },
  missing_marker:   { icon: '❌', color: 'var(--red)',    label: 'Missing markers',
                       help: 'An app claims them but no marker is stamped on disk.' },
  missing_file:     { icon: '🗑️', color: 'var(--red)',    label: 'Missing files',
                       help: 'An app claims the path but no file exists on disk.' },
  owned_ok:         { icon: '✓', color: 'var(--green)',   label: 'Owned & healthy',
                       help: 'Marker resolves to a live app that claims them.' },
};
const _RECON_BUCKET_ORDER = ['scrub_candidate', 'attach_candidate', 'missing_marker', 'missing_file', 'owned_ok'];

const _RECON_REASON_TEXT = {
  ineligible_path:                     'Path no app may own (telemetry / OC-standard file).',
  unresolvable_spec:                   'Marker points at an app that no longer exists.',
  marker_resolves_no_claim:            'Marker resolves to a live app that doesn\'t list it.',
  claimed_no_marker:                   'App claims it, but no marker on disk.',
  claimed_absent_on_disk:              'App claims it, but the file is gone.',
  claimed_and_marked:                  'Claimed and marked.',
  claimed_marker_resolved_via_lineage: 'Claimed; marker resolved via lineage.',
};

// Per-bucket count strip (dimmed when zero), mirrors _reflectSummaryItems.
function _reconSummaryItems(counts) {
  counts = counts || {};
  return _RECON_BUCKET_ORDER.map(b => {
    const meta = _RECON_BUCKET_META[b];
    const n = counts[b] || 0;
    const dim = n === 0 ? ';opacity:0.55' : '';
    return `<div style="font-size:0.78rem${dim}">
      <span style="color:${meta.color};font-weight:700">${meta.icon} ${n}</span>
      ${escHtml(meta.label)}
      <span style="color:var(--text3);font-size:0.7rem"> — ${escHtml(meta.help)}</span>
    </div>`;
  }).join('');
}

function _reconSection(bucket, n, inner) {
  const meta = _RECON_BUCKET_META[bucket];
  return `<div style="margin-bottom:14px">
    <div style="font-size:0.85rem;font-weight:700;margin-bottom:6px;color:${meta.color}">
      ${meta.icon} ${escHtml(meta.label)} (${n})
      <span style="color:var(--text3);font-size:0.7rem;font-weight:400"> — ${escHtml(meta.help)}</span>
    </div>${inner}
  </div>`;
}

function _reconTable(rows) {
  return `<table style="width:100%;border-collapse:collapse">
    <thead><tr style="color:var(--text3);font-size:0.65rem;text-transform:uppercase;letter-spacing:.06em">
      <th style="text-align:left;padding:4px 6px">Path</th>
      <th style="text-align:left;padding:4px 6px">Refs</th>
      <th style="text-align:left;padding:4px 6px">Why</th>
      <th style="text-align:right;padding:4px 6px;width:96px">Action</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function _reconRowHtml(r, actionCell) {
  return `<tr style="border-top:1px solid var(--border)">
    <td style="padding:5px 6px;color:var(--text1);font-family:monospace;font-size:0.72rem">${escHtml(r.path)}</td>
    <td style="padding:5px 6px;color:var(--text2);font-size:0.72rem">${r.spec_id ? `spec=${escHtml(r.spec_id)}` : '<span style="color:var(--text3)">—</span>'}</td>
    <td style="padding:5px 6px;color:var(--text3);font-size:0.72rem">${escHtml(_RECON_REASON_TEXT[r.reason] || r.reason || '')}</td>
    ${actionCell}
  </tr>`;
}

// Render the five buckets into grouped tables. scrub_candidate gets the new
// Strip action (per-row + "Strip all N"); attach_candidate / missing_marker
// reuse the existing index-based Attach / Fix handlers (we stash synthetic
// kind-findings so attachOrphanToInstance / applyReflectFix work unchanged);
// missing_file is operator-manual; owned_ok is a collapsed informational list.
function _renderReconBuckets(botId, recon) {
  const buckets = (recon && recon.buckets) || {};
  if (typeof window._reconRowsByBot !== 'object' || window._reconRowsByBot === null) window._reconRowsByBot = {};
  window._reconRowsByBot[botId] = buckets;
  if (typeof window._reflectFindingsByBot !== 'object' || window._reflectFindingsByBot === null) window._reflectFindingsByBot = {};

  // Synthetic kind-finding stash, so the existing (index-based) attach + fix
  // handlers can be reused for attach_candidate / missing_marker rows.
  const synth = [];
  const stash = (f) => { synth.push(f); return synth.length - 1; };

  const sections = [];

  const scrub = buckets.scrub_candidate || [];
  if (scrub.length) {
    const stripAll = `<div style="margin-bottom:8px">
        <button class="btn btn-warning btn-sm" data-bot="${escHtml(botId)}"
          onclick="stripAllScrubMarkers(this.dataset.bot)" style="font-size:0.72rem;padding:3px 10px">Strip all ${scrub.length}</button>
        <span style="font-size:0.68rem;color:var(--text3);margin-left:8px">Remove the marker from every file no app may own. File content is kept.</span>
      </div>`;
    const rows = scrub.map(r => _reconRowHtml(r,
      `<td style="padding:5px 6px;text-align:right">
         <button class="btn btn-warning btn-sm" data-bot="${escHtml(botId)}" data-path="${escHtml(r.path)}"
           onclick="stripScrubMarker(this.dataset.bot, this.dataset.path)" style="font-size:0.7rem;padding:2px 8px"
           title="Remove the evolve marker from this file (content preserved)">Strip marker</button>
       </td>`)).join('');
    sections.push(_reconSection('scrub_candidate', scrub.length, stripAll + _reconTable(rows)));
  }

  const attach = buckets.attach_candidate || [];
  if (attach.length) {
    const rows = attach.map(r => {
      const i = stash({ kind: 'orphan_file', file_path: r.abs_path, spec_id: r.spec_id });
      return _reconRowHtml(r,
        `<td style="padding:5px 6px;text-align:right">
           <button class="btn btn-ghost btn-sm" data-bot="${escHtml(botId)}" data-idx="${i}"
             onclick="attachOrphanToInstance(this.dataset.bot, parseInt(this.dataset.idx))" style="font-size:0.7rem;padding:2px 8px"
             title="Attach this file to the app its marker points at">Attach</button>
         </td>`);
    }).join('');
    sections.push(_reconSection('attach_candidate', attach.length, _reconTable(rows)));
  }

  const mm = buckets.missing_marker || [];
  if (mm.length) {
    const rows = mm.map(r => {
      const i = stash({ kind: 'missing_marker', file_path: r.abs_path, spec_id: r.spec_id,
        proposed_action: { kind: 'stamp_marker', spec_id: r.spec_id, file_id: r.file_id, spec_version: '' } });
      return _reconRowHtml(r,
        `<td style="padding:5px 6px;text-align:right">
           <button class="btn btn-ghost btn-sm" data-bot="${escHtml(botId)}" data-idx="${i}"
             onclick="applyReflectFix(this.dataset.bot, parseInt(this.dataset.idx))" style="font-size:0.7rem;padding:2px 8px">Fix</button>
         </td>`);
    }).join('');
    sections.push(_reconSection('missing_marker', mm.length, _reconTable(rows)));
  }

  const mf = buckets.missing_file || [];
  if (mf.length) {
    const rows = mf.map(r => _reconRowHtml(r,
      `<td style="padding:5px 6px;color:var(--text3);font-size:0.7rem;text-align:right" title="Operator decision — restore the file or remove the claim">manual</td>`)).join('');
    sections.push(_reconSection('missing_file', mf.length, _reconTable(rows)));
  }

  window._reflectFindingsByBot[botId] = synth;

  const owned = buckets.owned_ok || [];
  if (owned.length) {
    const list = owned.map(r => `<div style="font-family:monospace;font-size:0.72rem;color:var(--text2);padding:2px 0">${escHtml(r.path)}</div>`).join('');
    sections.push(`<details class="card" style="padding:10px 12px;margin-bottom:14px">
      <summary style="cursor:pointer;display:flex;align-items:center;gap:6px;font-size:0.85rem;font-weight:700;color:var(--green)">
        <span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
        ✓ Owned &amp; healthy (${owned.length})
      </summary>
      <div style="margin-top:8px">${list}</div>
    </details>`);
  }

  if (!sections.length) {
    return `<div style="color:var(--green);font-size:0.82rem;padding:14px 0;text-align:center">✓ Nothing to reconcile — every marked file resolves to a live app.</div>`;
  }
  return sections.join('');
}

// Summary card wrapping the per-bucket count strip. `lead` is optional HTML
// shown above the strip (e.g. the "scanned N files" line for the Reflect modal).
function _reconSummaryCard(recon, lead) {
  return `<div style="background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:10px 14px;margin-bottom:14px;display:flex;flex-direction:column;gap:4px">
    ${lead || ''}${_reconSummaryItems((recon && recon.counts) || {})}
  </div>`;
}

// Fetch the reflect endpoint (which carries `recon`) and render the five-bucket
// display into the slot element. Falls back to the legacy kind-based tables when
// the endpoint doesn't return buckets. Used by the Sync modal, whose own result
// shape is kind-based and can't distinguish scrub from attach on its own.
async function _injectReconBuckets(botId, slotId, fallbackFindings) {
  const slot = document.getElementById(slotId);
  if (!slot) return;
  let recon = null;
  try {
    const r = await fetch(`/api/bots/${encodeURIComponent(botId)}/reflect`);
    if (r.ok) recon = (await r.json()).recon;
  } catch (_e) { /* fall through to legacy */ }
  if (recon && recon.buckets) {
    slot.innerHTML = _reconSummaryCard(recon) + _renderReconBuckets(botId, recon);
    return;
  }
  slot.innerHTML = (fallbackFindings && fallbackFindings.length)
    ? _reflectFindingsTables(botId, fallbackFindings)
    : `<div style="color:var(--green);font-size:0.82rem;padding:14px 0;text-align:center">✓ No manifest hygiene issues found.</div>`;
}

// Re-render the open Reflect/Sync surface after an INLINE DRIFT MUTATION
// (Attach / Strip / Fix). These are cheap, targeted manifest writes, so the
// refresh MUST re-check via the read-only reflect path — NEVER the
// discovery-escalating sync_bot. Routing a drift refresh through runSync()
// re-ran the full ~60s 8-phase LLM discovery on every click, because the file
// the action just wrote/changed reads as fresh on-disk evidence and tips
// sync_bot over its uncovered-escalation threshold. The recon buckets a Strip
// or Attach changes are exactly what runReflect()/runReflectPod() (GET reflect,
// no LLM) render, so the cheap path shows the same updated content sub-second.
function _refreshReflectSurface(botId) {
  if (window._reflectMode === 'sync-pod' || window._reflectMode === 'pod') runReflectPod();
  else runReflect(window._reflectActiveBotId || botId);
}

// Strip the evolve marker off one scrub-candidate file. Destructive-but-
// reversible (re-stamp via the app), so confirm with danger styling. On 403 the
// evolve user lacked write ACL — surface the manual_cli for an SSH run, exactly
// like applyReflectFix.
async function stripScrubMarker(botId, path) {
  const ok = await confirmModal({
    title: 'Strip marker',
    body: `Strip the evolve marker from:\n\n${path}\n\nThe file content is kept — only the provenance marker is removed.`,
    confirmLabel: 'Strip marker',
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await fetch(`/api/bots/${encodeURIComponent(botId)}/reflect/strip-marker`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ path }),
    });
    const data = await r.json();
    if (r.status === 403 && data.manual_cli) {
      try { await navigator.clipboard.writeText(data.manual_cli); } catch (_e) { /* clipboard may be blocked */ }
      // data.error carries the server's diagnosis + remedy (e.g. dormant
      // sudoers grant → `sudo evolve-admin refresh-sudoers`) — never hide it.
      const diagnosis = data.error ? `\n\n${data.error}` : '';
      toast(`Direct write blocked.${diagnosis}\n\nCopy this CLI and run via SSH:\n\n${data.manual_cli}\n\n(Copied to clipboard if your browser allows it.)`, 'err');
      return;
    }
    if (!r.ok) { toast('✗ ' + (data.error || `Strip failed (HTTP ${r.status})`), 'err'); return; }
    toast(data.stripped ? '✓ Marker stripped' : '✓ No marker to strip', 'ok');
    _refreshReflectSurface(botId);
  } catch (e) {
    toast('✗ Strip failed: ' + e, 'err');
  }
}

// Bulk-strip every scrub candidate for the bot. Per-row permission failures
// come back in `blocked` (each with a manual_cli) rather than aborting.
async function stripAllScrubMarkers(botId) {
  const n = (((window._reconRowsByBot || {})[botId] || {}).scrub_candidate || []).length;
  if (!n) { toast('Nothing to strip', 'info'); return; }
  const ok = await confirmModal({
    title: 'Strip all markers',
    body: `Strip the evolve marker from ${n} file${n === 1 ? '' : 's'} no app may own?\n\nFile contents are kept — only the markers are removed.`,
    confirmLabel: `Strip all ${n}`,
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await fetch(`/api/bots/${encodeURIComponent(botId)}/reflect/strip-markers`, { method: 'POST' });
    const data = await r.json();
    if (!r.ok) { toast('✗ ' + (data.error || `Strip failed (HTTP ${r.status})`), 'err'); return; }
    const blocked = (data.blocked || []).length;
    if (blocked) {
      const first = data.blocked[0] || {};
      if (first.manual_cli) { try { await navigator.clipboard.writeText(first.manual_cli); } catch (_e) { /* clipboard may be blocked */ } }
      // Blocked rows share one root cause — the endpoint's only blocked
      // producer is the filesystem-ACL PermissionError branch — so the first
      // row's server diagnosis speaks for all; surface it, not a bare count.
      const diagnosis = first.error ? `\n\n${first.error}` : '';
      const cliNote = first.manual_cli
        ? `\n\nFirst blocked file's SSH command:\n\n${first.manual_cli}\n\n(Copied to clipboard if your browser allows it.)`
        : '';
      toast(`✓ Stripped ${data.stripped_count}; ${blocked} file${blocked === 1 ? '' : 's'} blocked — bot-user privileges needed.${diagnosis}${cliNote}`, 'warn');
    } else {
      toast(`✓ Stripped ${data.stripped_count} marker${data.stripped_count === 1 ? '' : 's'}`, 'ok');
    }
    _refreshReflectSurface(botId);
  } catch (e) {
    toast('✗ Strip failed: ' + e, 'err');
  }
}

// Swap the shared results modal's title + subtitle between the Sync and
// Reflect framings (the same modal element backs both).
function _setReflectModalChrome(mode) {
  const titleEl = document.getElementById('reflect-modal-title');
  const subEl = document.getElementById('reflect-modal-sub');
  if (titleEl) {
    titleEl.textContent = mode === 'sync-pod' ? 'Sync Applications — Pod'
      : mode === 'sync' ? 'Sync Applications'
      : mode === 'pod' ? 'Reflect — Manifest Hygiene (Pod)'
      : 'Reflect — Manifest Hygiene';
  }
  if (subEl) {
    subEl.textContent = (mode === 'sync' || mode === 'sync-pod')
      ? 'One pass: discover new workspace apps (generating manifests when found) and audit each manifest against what\'s on disk. Drift below can be fixed inline.'
      : 'v7-arc Instance scan: detects files whose marker doesn\'t match what any Instance claims, files that should have a marker but don\'t, stale v6 pkg= markers that the migration missed, and paths an Instance claims but that don\'t exist on disk. Read-only — no changes applied.';
  }
}

async function runReflect(botId) {
  if (!botId) {
    toast('Select a bot first', 'err');
    return;
  }
  _setReflectModalChrome('reflect');
  const modal = document.getElementById('reflect-modal');
  const body = document.getElementById('reflect-modal-body');
  body.innerHTML = `<div style="color:var(--text2);padding:20px 0;text-align:center">Scanning ${escHtml(botId)}…</div>`;
  modal.classList.add('open');
  // Set mode flag so applyReflectFix knows to refresh via runReflect(botId)
  // rather than runReflectPod. Mirrors the same flag in runReflectPod.
  window._reflectMode = 'per-bot';
  window._reflectActiveBotId = botId;

  let data;
  try {
    const r = await fetch(`/api/bots/${encodeURIComponent(botId)}/reflect`);
    if (!r.ok) {
      const err = (await r.json().catch(() => ({}))).error || `HTTP ${r.status}`;
      body.innerHTML = `<div style="color:var(--red);padding:12px 0">Scan failed: ${escHtml(err)}</div>`;
      return;
    }
    data = await r.json();
  } catch (e) {
    body.innerHTML = `<div style="color:var(--red);padding:12px 0">Scan failed: ${escHtml(String(e))}</div>`;
    return;
  }

  const findings = data.findings || [];
  const counts = data.counts || {};
  const warnings = data.warnings || [];

  // Warnings (non-fatal scan-time issues)
  const warningsHtml = warnings.length ? `<div style="margin-bottom:14px">
    ${warnings.map(w => `<div style="font-size:0.75rem;color:var(--yellow);margin-bottom:3px">⚠ ${escHtml(w)}</div>`).join('')}
  </div>` : '';

  // Preferred path: render the authoritative five-bucket reconciliation display
  // (distinguishes scrub_candidate → Strip from attach_candidate → Attach).
  const recon = data.recon;
  if (recon && recon.buckets) {
    const lead = `<div style="font-size:0.72rem;color:var(--text2);margin-bottom:2px">
      Scanned ${data.files_scanned} file${data.files_scanned === 1 ? '' : 's'}
      across ${data.instances_checked} v7-arc Instance${data.instances_checked === 1 ? '' : 's'} for <strong>${escHtml(data.bot_id)}</strong>
    </div>`;
    body.innerHTML = _reconSummaryCard(recon, lead) + warningsHtml + _renderReconBuckets(data.bot_id, recon);
    return;
  }

  // Fallback: legacy kind-based tables (endpoint didn't return recon buckets).
  const summaryHtml = `<div style="background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:10px 14px;margin-bottom:14px;display:flex;flex-direction:column;gap:4px">
    <div style="font-size:0.72rem;color:var(--text2);margin-bottom:2px">
      Scanned ${data.files_scanned} file${data.files_scanned === 1 ? '' : 's'}
      across ${data.instances_checked} v7-arc Instance${data.instances_checked === 1 ? '' : 's'} for <strong>${escHtml(data.bot_id)}</strong>
    </div>
    ${_reflectSummaryItems(counts)}
    ${_reflectReconcileBtn(data.bot_id, counts)}
  </div>`;

  if (!findings.length) {
    _reflectFindingsTables(data.bot_id, []);  // clear stash for this bot
    body.innerHTML = summaryHtml + warningsHtml +
      `<div style="color:var(--green);font-size:0.82rem;padding:14px 0;text-align:center">✓ No manifest hygiene issues found.</div>`;
    return;
  }

  body.innerHTML = summaryHtml + warningsHtml + _reflectFindingsTables(data.bot_id, findings);
}

// ── Sync apps: the one smart maintenance action ──────────────────────────────
// Replaces the old co-equal "App Scan" + "Reflect" buttons. POSTs to the
// /sync endpoint, which decides whether a full LLM scan is needed (cheap vs
// escalated) and returns a unified result: discovery counts + drift_findings
// (same shape Reflect returns). On the escalated path the endpoint runs the
// scan in-process — writing .scan-status.json exactly like /scan — so we poll
// that status file concurrently and reuse the App-Scan phase tracker for the
// long-running indicator.
//
// NB: depends on the sibling chip that adds POST /api/applications/sync
// (per-bot) and its pod-wide variant. Coded against the frozen response shape.
async function runSync(botId) {
  if (!botId) { toast('Select a bot first', 'err'); return; }
  const modal = document.getElementById('reflect-modal');
  const body = document.getElementById('reflect-modal-body');
  _setReflectModalChrome('sync');
  modal.classList.add('open');
  // Refresh routing: a Fix/Attach/Reconcile applied from this surface should
  // re-run sync (cheap path — fast) rather than the read-only reflect.
  window._reflectMode = 'sync';
  window._reflectActiveBotId = botId;
  body.innerHTML = `<div style="color:var(--text2);padding:20px 0;text-align:center">Checking ${escHtml(botLabel(botId))}…</div>`;

  // Poll the scan-status file. If /sync escalates to a full scan it writes the
  // same .scan-status.json /scan does, so we can surface the phase tracker.
  let settled = false;
  let escalatedShown = false;
  _scanStartTime = null;
  if (window._syncPollTimer) { clearInterval(window._syncPollTimer); window._syncPollTimer = null; }
  window._syncPollTimer = setInterval(async () => {
    if (settled) return;
    let s;
    try { s = await api('GET', `/api/applications/scan/status?bot=${encodeURIComponent(botId)}`); }
    catch (_e) { return; }
    if (settled || !s || s.status !== 'running') return;
    if (!escalatedShown) { escalatedShown = true; _scanStartTime = Date.now(); }
    body.innerHTML =
      `<div style="font-weight:600;font-size:0.9rem;margin-bottom:4px;text-align:center">New apps found — scanning…</div>` +
      _renderScanProgress(s);
    const logBtn = document.getElementById('cap-scan-log-btn');
    if (logBtn && s.log) logBtn.style.display = 'inline-block';
  }, 2500);

  let data;
  try {
    const r = await fetch(`/api/applications/sync?bot=${encodeURIComponent(botId)}`, { method: 'POST' });
    if (!r.ok) {
      settled = true; clearInterval(window._syncPollTimer); window._syncPollTimer = null; _scanStartTime = null;
      const err = (await r.json().catch(() => ({}))).error || `HTTP ${r.status}`;
      body.innerHTML = `<div style="color:var(--red);padding:12px 0">Sync failed: ${escHtml(err)}</div>`;
      return;
    }
    data = await r.json();
  } catch (e) {
    settled = true; clearInterval(window._syncPollTimer); window._syncPollTimer = null; _scanStartTime = null;
    body.innerHTML = `<div style="color:var(--red);padding:12px 0">Sync failed: ${escHtml(String(e))}</div>`;
    return;
  }
  settled = true;
  clearInterval(window._syncPollTimer); window._syncPollTimer = null; _scanStartTime = null;
  _renderSyncResult(botId, data);
}

// Render the unified /sync result into the shared modal: a status headline
// (cheap = "Up to date", escalated = "Found N new apps"), the per-kind drift
// strip, and the SAME findings table Reflect uses.
function _renderSyncResult(botId, data) {
  const body = document.getElementById('reflect-modal-body');
  data = data || {};
  const path = data.path === 'escalated' ? 'escalated' : 'cheap';
  const findings = data.drift_findings || [];
  const discovered = data.discovered_count || 0;
  const uncovered = data.uncovered_files || [];
  const driftTotal = findings.length;

  const headline = path === 'escalated'
    ? `Found ${discovered} new app${discovered === 1 ? '' : 's'} — scan complete`
    : `Up to date — ${driftTotal} hygiene issue${driftTotal === 1 ? '' : 's'}`;
  toast(`✓ ${headline}`, 'ok');

  const reasonLine = data.reason
    ? `<div style="font-size:0.7rem;color:var(--text3);margin-top:2px">${escHtml(data.reason)}</div>` : '';
  const uncoveredLine = uncovered.length
    ? `<div style="font-size:0.7rem;color:var(--text3);margin-top:4px" title="${escHtml(uncovered.join('\n'))}">${uncovered.length} workspace file${uncovered.length === 1 ? '' : 's'} not yet part of any app</div>`
    : '';

  const summaryHtml = `<div style="background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:10px 14px;margin-bottom:14px;display:flex;flex-direction:column;gap:4px">
    <div style="font-size:0.85rem;font-weight:700;color:var(--text1)">${escHtml(headline)} — <strong>${escHtml(botLabel(botId))}</strong></div>
    ${reasonLine}
    ${uncoveredLine}
  </div>`;

  // Drift renders through the authoritative five-bucket reconciliation display
  // (so scrub_candidates offer Strip, not the corrupting Attach). The sync
  // result is kind-based and can't distinguish the two, so we fetch the recon
  // ledger here; `findings` is the legacy fallback if buckets are unavailable.
  body.innerHTML = summaryHtml +
    `<div id="recon-buckets-slot"><div style="color:var(--text2);padding:14px 0;text-align:center">Reconciling…</div></div>`;
  _injectReconBuckets(botId, 'recon-buckets-slot', findings);

  // A full scan may have minted new apps / manifests — refresh the page grid.
  if (path === 'escalated' && typeof loadCapabilities === 'function') {
    loadCapabilities();
  }
}

// Run the auto-reconcile that converts orphan_file findings into
// Instance.realized_files[] entries. Two-step: dry-run first to show the
// breakdown (resolved / ambiguous / unmatched), confirm, then apply.
async function reconcileOrphanMarkers(botId) {
  // Step 1: dry-run preview.
  let preview;
  try {
    const r = await fetch(`/api/bots/${encodeURIComponent(botId)}/reflect/reconcile`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({apply: false}),
    });
    if (!r.ok) {
      const err = (await r.json().catch(() => ({}))).error || `HTTP ${r.status}`;
      toast('✗ Reconcile preview failed: ' + err, 'err');
      return;
    }
    preview = await r.json();
  } catch (e) {
    toast('✗ Reconcile preview failed: ' + e, 'err');
    return;
  }

  const nResolved = (preview.resolved || []).length;
  const nAmbiguous = (preview.ambiguous || []).length;
  const nUnmatched = (preview.unmatched || []).length;
  if (nResolved === 0) {
    const msg = nAmbiguous + nUnmatched > 0
      ? `Nothing to reconcile automatically. ${nAmbiguous} ambiguous + ${nUnmatched} unmatched need operator triage.`
      : 'Nothing to reconcile.';
    toast(msg, 'info');
    return;
  }

  const summary = [
    `Attach ${nResolved} orphan file${nResolved === 1 ? '' : 's'} to their matching Instances on ${botId}?`,
    '',
    `  resolved:   ${nResolved}  (will be attached)`,
    nAmbiguous ? `  ambiguous:  ${nAmbiguous}  (skipped — multiple Instances claim the spec_id)` : '',
    nUnmatched ? `  unmatched:  ${nUnmatched}  (skipped — no Instance for the spec_id)` : '',
  ].filter(Boolean).join('\n');
  if (!await confirmModal(summary)) return;

  // Step 2: apply.
  try {
    const r = await fetch(`/api/bots/${encodeURIComponent(botId)}/reflect/reconcile`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({apply: true}),
    });
    const data = await r.json();
    if (!r.ok) {
      toast('✗ Reconcile failed: ' + (data.error || `HTTP ${r.status}`), 'err');
      return;
    }
    const n = (data.resolved || []).length;
    toast(`✓ Reconciled ${n} orphan${n === 1 ? '' : 's'}`, 'ok');
    // Cheap read-only re-check — never the discovery-escalating sync_bot.
    _refreshReflectSurface(botId);
  } catch (e) {
    toast('✗ Reconcile failed: ' + e, 'err');
  }
}

// Per-row Attach. TARGETED: sends only this file's path, so the endpoint does a
// single Instance read-mutate-write (no full-bot reconcile) and the refresh is
// the cheap read-only reflect re-check — sub-second, never a discovery rescan.
async function attachOrphanToInstance(botId, findingIdx) {
  const findings = (window._reflectFindingsByBot || {})[botId];
  if (!findings || !findings[findingIdx]) {
    toast('Stale finding — please rerun Reflect first', 'err');
    return;
  }
  const targetPath = findings[findingIdx].file_path;
  try {
    const r = await fetch(`/api/bots/${encodeURIComponent(botId)}/reflect/reconcile`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({apply: true, paths: [targetPath]}),
    });
    const data = await r.json();
    if (!r.ok) {
      toast('✗ Attach failed: ' + (data.error || `HTTP ${r.status}`), 'err');
      return;
    }
    const hit = (data.resolved || []).find(x => x.file_path === targetPath);
    if (hit) {
      toast(`✓ Attached to ${hit.instance_id}`, 'ok');
    } else {
      // Either ambiguous or unmatched — surface why.
      const amb = (data.ambiguous || []).find(x => x.file_path === targetPath);
      const unm = (data.unmatched || []).find(x => x.file_path === targetPath);
      if (amb) toast(`✗ Ambiguous — ${amb.instance_ids.length} Instances claim ${amb.spec_id}`, 'err');
      else if (unm) toast('✗ No longer an attach candidate — rerun the scan', 'err');
      else toast('✗ Attach not applied (file no longer present?)', 'err');
    }
    _refreshReflectSurface(botId);
  } catch (e) {
    toast('✗ Attach failed: ' + e, 'err');
  }
}

// Apply a Reflect finding's fix by POSTing the proposed_action to the server.
// On 403 (PermissionError on the file write because evolve lacks ACL write on
// the path), surface the manual_cli command in a toast so the operator can run
// it via SSH. On 200, rerun the scan so the row clears.
async function applyReflectFix(botId, findingIdx) {
  const findings = (window._reflectFindingsByBot || {})[botId];
  if (!findings || !findings[findingIdx]) {
    toast('Stale finding — please rerun Reflect first', 'err');
    return;
  }
  const finding = findings[findingIdx];
  const action = finding.proposed_action || {};
  const body = {
    kind: action.kind,
    file_path: finding.file_path,
    spec_id: action.spec_id || finding.spec_id,
    spec_version: action.spec_version || '',
    file_id: action.file_id || '',
  };

  try {
    const r = await fetch(`/api/bots/${encodeURIComponent(botId)}/reflect/apply-fix`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (r.status === 403 && data.manual_cli) {
      // PermissionError — surface the CLI command via a copy-to-clipboard prompt.
      // data.error carries the server's diagnosis + remedy (e.g. dormant
      // sudoers grant → `sudo evolve-admin refresh-sudoers`) — never hide it.
      const diagnosis = data.error ? `\n\n${data.error}` : '';
      const msg = `Direct write blocked.${diagnosis}\n\nCopy this CLI command and run via SSH:\n\n${data.manual_cli}`;
      try { await navigator.clipboard.writeText(data.manual_cli); }
      catch (e) { /* clipboard may not be available */ }
      toast(msg + '\n\n(Copied to clipboard if your browser allows it.)', 'err');
      return;
    }
    if (!r.ok) {
      toast('✗ ' + (data.error || `Fix failed (HTTP ${r.status})`), 'err');
      return;
    }
    toast(`✓ Fix applied: ${data.kind}`, 'ok');
    // Cheap read-only re-check — an inline fix is a targeted manifest write, so
    // it must not trigger the discovery-escalating sync_bot.
    _refreshReflectSurface(botId);
  } catch (e) {
    toast('✗ Fix failed: ' + e, 'err');
  }
}

function closeReflectModal() {
  document.getElementById('reflect-modal')?.classList.remove('open');
}

// Pod-wide Reflect rollup — runs reflect across every bot in network.json and
// aggregates findings into the same modal. Per-bot subsections are rendered
// below the aggregate header so an operator can see both totals and which
// bot owns each finding.
async function runReflectPod() {
  const modal = document.getElementById('reflect-modal');
  const body = document.getElementById('reflect-modal-body');
  _setReflectModalChrome('pod');
  body.innerHTML = `<div style="color:var(--text2);padding:20px 0;text-align:center">Scanning pod…</div>`;
  modal.classList.add('open');
  // Track the active Reflect surface so applyReflectFix can pick the right
  // refresh path (per-bot view → runReflect, pod view → runReflectPod).
  window._reflectMode = 'pod';

  let data;
  try {
    const r = await fetch(`/api/reflect/pod`);
    if (!r.ok) {
      const err = (await r.json().catch(() => ({}))).error || `HTTP ${r.status}`;
      body.innerHTML = `<div style="color:var(--red);padding:12px 0">Scan failed: ${escHtml(err)}</div>`;
      return;
    }
    data = await r.json();
  } catch (e) {
    body.innerHTML = `<div style="color:var(--red);padding:12px 0">Scan failed: ${escHtml(String(e))}</div>`;
    return;
  }

  const counts = data.aggregate_counts || {};
  const bots = data.bots || [];

  // Stash per-bot findings so applyReflectFix can recover proposed_action by
  // bot_id + index (same pattern as runReflect). Reset to capture only this
  // pod scan's findings — stale bots wouldn't matter, but tidy.
  if (typeof window._reflectFindingsByBot !== 'object' || window._reflectFindingsByBot === null) {
    window._reflectFindingsByBot = {};
  }
  for (const b of bots) {
    if (b.bot_id && b.findings) {
      window._reflectFindingsByBot[b.bot_id] = b.findings;
    }
  }
  // Fixable-kind set — defined locally too so a pod-modal click works
  // whether or not the per-bot modal has been opened first in this session.
  const _REFLECT_FIXABLE_POD = new Set(['stamp_marker', 'rewrite_marker_to_spec']);

  // Aggregate summary — same per-kind rows as the per-bot view, prefixed
  // with pod-wide totals.
  const summaryItems = ['orphan_file', 'missing_marker', 'stale_pkg_marker', 'missing_disk_file']
    .map(kind => {
      const meta = _REFLECT_KIND_META[kind];
      const n = counts[kind] || 0;
      const dim = n === 0 ? ';opacity:0.55' : '';
      return `<div style="font-size:0.78rem${dim}">
        <span style="color:${meta.color};font-weight:700">${meta.icon} ${n}</span>
        ${escHtml(meta.label)}
        <span style="color:var(--text3);font-size:0.7rem"> — ${escHtml(meta.help)}</span>
      </div>`;
    }).join('');
  const summaryHtml = `<div style="background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:10px 14px;margin-bottom:14px;display:flex;flex-direction:column;gap:4px">
    <div style="font-size:0.72rem;color:var(--text2);margin-bottom:2px">
      <strong>Pod-wide scan</strong> · ${bots.length} bot${bots.length === 1 ? '' : 's'} ·
      ${data.total_files_scanned} file${data.total_files_scanned === 1 ? '' : 's'} scanned ·
      ${data.total_instances_checked} Instance${data.total_instances_checked === 1 ? '' : 's'} checked ·
      <strong>${data.total_findings}</strong> total finding${data.total_findings === 1 ? '' : 's'}
    </div>
    ${summaryItems}
  </div>`;

  if (data.total_findings === 0 && !bots.some(b => b.error)) {
    body.innerHTML = summaryHtml +
      `<div style="color:var(--green);font-size:0.82rem;padding:14px 0;text-align:center">✓ No manifest hygiene issues anywhere in the pod.</div>`;
    return;
  }

  // Per-bot breakdown — each bot gets a small section with its own counts
  // line and a findings table grouped by kind. Bots with zero findings show
  // a compact "clean" row instead of empty tables.
  const botSections = bots.map(b => {
    if (b.error) {
      return `<div style="margin-bottom:14px;padding:8px 12px;background:var(--bg2);border-left:3px solid var(--red);border-radius:4px">
        <div style="font-size:0.85rem;font-weight:700">${escHtml(botLabel(b.bot_id))}</div>
        <div style="font-size:0.75rem;color:var(--red);margin-top:4px">Scan failed: ${escHtml(b.error)}</div>
      </div>`;
    }
    const findings = b.findings || [];
    if (findings.length === 0) {
      return `<div style="margin-bottom:8px;padding:6px 12px;background:var(--bg2);border-radius:4px;font-size:0.78rem">
        <strong>${escHtml(botLabel(b.bot_id))}</strong>
        <span style="color:var(--green);margin-left:6px">✓ clean</span>
        <span style="color:var(--text3);margin-left:6px">(${b.files_scanned} files / ${b.instances_checked} instances)</span>
      </div>`;
    }
    // Group + render same way as the per-bot view
    const byKind = {};
    for (const f of findings) {
      (byKind[f.kind] = byKind[f.kind] || []).push(f);
    }
    const orphanCountPod = (b.counts || {}).orphan_file || 0;
    const reconcileBtnPod = orphanCountPod > 0
      ? `<button class="btn btn-primary btn-sm" data-bot="${escHtml(b.bot_id)}"
           onclick="reconcileOrphanMarkers(this.dataset.bot)"
           style="font-size:0.68rem;padding:2px 8px;margin-left:8px"
           title="Attach orphans to their matching Instances">
           Reconcile (${orphanCountPod})
         </button>`
      : '';
    const _REFLECT_RECONCILABLE_POD = new Set(['orphan_file']);
    const tables = Object.keys(byKind).map(kind => {
      const meta = _REFLECT_KIND_META[kind] || { icon: '•', color: 'var(--text2)', label: kind };
      const fixable = _REFLECT_FIXABLE_POD.has(kind);
      const reconcilable = _REFLECT_RECONCILABLE_POD.has(kind);
      const rows = byKind[kind].map(f => {
        const idx = findings.indexOf(f);
        let fixCell;
        if (fixable) {
          fixCell = `<td style="padding:4px 6px;text-align:right">
               <button class="btn btn-ghost btn-sm" data-bot="${escHtml(b.bot_id)}" data-idx="${idx}" onclick="applyReflectFix(this.dataset.bot, parseInt(this.dataset.idx))" style="font-size:0.68rem;padding:1px 7px">Fix</button>
             </td>`;
        } else if (reconcilable) {
          fixCell = `<td style="padding:4px 6px;text-align:right">
               <button class="btn btn-ghost btn-sm" data-bot="${escHtml(b.bot_id)}" data-idx="${idx}" onclick="attachOrphanToInstance(this.dataset.bot, parseInt(this.dataset.idx))" style="font-size:0.68rem;padding:1px 7px" title="Attach this file to the Instance whose spec_id matches its marker">Attach</button>
             </td>`;
        } else {
          fixCell = `<td style="padding:4px 6px;color:var(--text3);font-size:0.68rem;text-align:right" title="Operator decision — manual action required">manual</td>`;
        }
        return `<tr style="border-top:1px solid var(--border)">
          <td style="padding:4px 6px;color:var(--text1);font-family:monospace;font-size:0.7rem">${escHtml(f.file_path)}</td>
          <td style="padding:4px 6px;color:var(--text2);font-size:0.7rem">
            ${f.spec_id ? `spec=${escHtml(f.spec_id)}` : ''}
            ${f.instance_id ? `<br>instance=${escHtml(f.instance_id)}` : ''}
          </td>
          ${fixCell}
        </tr>`;
      }).join('');
      return `<div style="margin-top:6px">
        <div style="font-size:0.78rem;font-weight:700;color:${meta.color};margin-bottom:4px">
          ${meta.icon} ${escHtml(meta.label)} (${byKind[kind].length})
        </div>
        <table style="width:100%;border-collapse:collapse">${rows}</table>
      </div>`;
    }).join('');
    return `<div style="margin-bottom:14px;padding:10px 12px;background:var(--bg2);border-left:3px solid var(--blue);border-radius:4px">
      <div style="font-size:0.85rem;font-weight:700">${escHtml(botLabel(b.bot_id))}
        <span style="color:var(--text3);font-size:0.7rem;font-weight:normal">(${findings.length} finding${findings.length === 1 ? '' : 's'})</span>
        ${reconcileBtnPod}
      </div>
      ${tables}
    </div>`;
  }).join('');

  body.innerHTML = summaryHtml + botSections;
}

// Pod-wide Sync rollup — the pod variant of runSync. Calls the pod-wide /sync
// endpoint and renders each bot's drift through the SAME shared findings table
// the per-bot view uses, prefixed with a pod-level discovery + drift rollup.
//
// NB: the per-bot /sync response shape is frozen; the pod-wide variant's shape
// is read defensively here (bots[] of per-bot sync results) pending the sibling
// endpoint chip.
async function runSyncPod() {
  const modal = document.getElementById('reflect-modal');
  const body = document.getElementById('reflect-modal-body');
  _setReflectModalChrome('sync-pod');
  body.innerHTML = `<div style="color:var(--text2);padding:20px 0;text-align:center">Syncing pod…</div>`;
  modal.classList.add('open');
  window._reflectMode = 'sync-pod';

  let data;
  try {
    const r = await fetch(`/api/applications/sync/pod`, { method: 'POST' });
    if (!r.ok) {
      const err = (await r.json().catch(() => ({}))).error || `HTTP ${r.status}`;
      body.innerHTML = `<div style="color:var(--red);padding:12px 0">Sync failed: ${escHtml(err)}</div>`;
      return;
    }
    data = await r.json();
  } catch (e) {
    body.innerHTML = `<div style="color:var(--red);padding:12px 0">Sync failed: ${escHtml(String(e))}</div>`;
    return;
  }

  const bots = data.bots || data.results || [];
  // Roll up totals across bots (computed client-side if the endpoint doesn't
  // pre-aggregate — keeps the rollup correct under either shape).
  const aggCounts = data.aggregate_drift_counts || (() => {
    const acc = {};
    for (const b of bots) {
      const c = b.drift_counts || {};
      for (const k of Object.keys(c)) acc[k] = (acc[k] || 0) + (c[k] || 0);
    }
    return acc;
  })();
  const totalDiscovered = bots.reduce((n, b) => n + (b.discovered_count || 0), 0);
  const totalDrift = bots.reduce((n, b) => n + ((b.drift_findings || []).length), 0);
  const escalated = bots.filter(b => b.path === 'escalated').length;

  const summaryHtml = `<div style="background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:10px 14px;margin-bottom:14px;display:flex;flex-direction:column;gap:4px">
    <div style="font-size:0.72rem;color:var(--text2);margin-bottom:2px">
      <strong>Pod-wide sync</strong> · ${bots.length} bot${bots.length === 1 ? '' : 's'} ·
      <strong>${totalDiscovered}</strong> new app${totalDiscovered === 1 ? '' : 's'} ·
      ${escalated} full scan${escalated === 1 ? '' : 's'} ·
      <strong>${totalDrift}</strong> drift finding${totalDrift === 1 ? '' : 's'}
    </div>
    ${_reflectSummaryItems(aggCounts)}
  </div>`;

  if (totalDrift === 0 && totalDiscovered === 0 && !bots.some(b => b.error)) {
    body.innerHTML = summaryHtml +
      `<div style="color:var(--green);font-size:0.82rem;padding:14px 0;text-align:center">✓ Every bot is up to date — no new apps, no manifest drift.</div>`;
    return;
  }

  const botSections = bots.map(b => {
    if (b.error) {
      return `<div style="margin-bottom:14px;padding:8px 12px;background:var(--bg2);border-left:3px solid var(--red);border-radius:4px">
        <div style="font-size:0.85rem;font-weight:700">${escHtml(botLabel(b.bot_id))}</div>
        <div style="font-size:0.75rem;color:var(--red);margin-top:4px">Sync failed: ${escHtml(b.error)}</div>
      </div>`;
    }
    const findings = b.drift_findings || [];
    const discovered = b.discovered_count || 0;
    const note = b.path === 'escalated'
      ? `${discovered} new app${discovered === 1 ? '' : 's'}`
      : 'up to date';
    if (findings.length === 0) {
      return `<div style="margin-bottom:8px;padding:6px 12px;background:var(--bg2);border-radius:4px;font-size:0.78rem">
        <strong>${escHtml(botLabel(b.bot_id))}</strong>
        <span style="color:var(--green);margin-left:6px">✓ ${escHtml(note)}</span>
        <span style="color:var(--text3);margin-left:6px">— no drift</span>
      </div>`;
    }
    return `<div style="margin-bottom:14px;padding:10px 12px;background:var(--bg2);border-left:3px solid var(--blue);border-radius:4px">
      <div style="font-size:0.85rem;font-weight:700;margin-bottom:6px">${escHtml(botLabel(b.bot_id))}
        <span style="color:var(--text3);font-size:0.7rem;font-weight:normal">(${escHtml(note)} · ${findings.length} drift finding${findings.length === 1 ? '' : 's'})</span>
        ${_reflectReconcileBtn(b.bot_id, b.drift_counts || {})}
      </div>
      ${_reflectFindingsTables(b.bot_id, findings)}
    </div>`;
  }).join('');

  body.innerHTML = summaryHtml + botSections;

  // A full scan on any bot may have minted new apps — refresh the page grid.
  if (escalated > 0 && typeof loadCapabilities === 'function') {
    loadCapabilities();
  }
}

// ── Applications-toolbar overflow menu ───────────────────────────────────────
// Toggles the "More" popover that holds the demoted advanced actions (force
// rescan / re-check / pod sync). Modeled on the pod-tile menu: toggle on the
// trigger, close on outside click or after picking an item.
function _appsToolMenuToggle(btn) {
  const wrap = btn.closest('.apps-toolmenu');
  if (!wrap) return;
  const open = wrap.classList.toggle('is-open');
  if (open) {
    const close = (e) => {
      if (!wrap.contains(e.target)) {
        wrap.classList.remove('is-open');
        document.removeEventListener('click', close, true);
      }
    };
    setTimeout(() => document.addEventListener('click', close, true), 0);
  }
}

function _appsToolMenuPick(itemEl, fn) {
  const wrap = itemEl.closest('.apps-toolmenu');
  if (wrap) wrap.classList.remove('is-open');
  try { fn(); } catch (e) { console.error('apps tool-menu action failed', e); }
}

// ── Adopt: rebind an Instance to a newer Spec version (v7-arc §8.1.5) ────────
// State for the open Adopt modal — captured at open so refresh/race guards work.
let _adoptBotId = null, _adoptAppId = null, _adoptAppName = null;
let _adoptPreviewData = null;

async function openAdoptModal(botId, appId, appName) {
  _adoptBotId = botId;
  _adoptAppId = appId;
  _adoptAppName = appName || appId;
  _adoptPreviewData = null;

  const body = document.getElementById('adopt-modal-body');
  body.innerHTML = `<div style="color:var(--text2);padding:20px 0;text-align:center">Loading diff…</div>`;
  document.getElementById('adopt-modal').classList.add('open');

  // Split fetch from render so a render-side ReferenceError doesn't masquerade
  // as "Failed to load" (PR #1671 — _relTime undef looked like an API failure).
  let data;
  try {
    const r = await fetch(`/api/applications/${encodeURIComponent(botId)}/${encodeURIComponent(appId)}/adopt-preview`);
    data = await r.json();
    // Guard: user may have closed the modal during the fetch
    if (_adoptBotId !== botId || _adoptAppId !== appId) return;
    if (!r.ok) {
      body.innerHTML = `<div style="color:var(--red);padding:12px 0">${escHtml(data.error || `HTTP ${r.status}`)}</div>`;
      return;
    }
  } catch (e) {
    body.innerHTML = `<div style="color:var(--red);padding:12px 0">Failed to load: ${escHtml(String(e))}</div>`;
    return;
  }
  _adoptPreviewData = data;
  _renderAdoptBody(data);
}

function _renderAdoptBody(data) {
  const body = document.getElementById('adopt-modal-body');
  if (!body) return;
  const diff = data.spec_diff || {};
  const kind = diff.kind || 'unknown';

  // Header: app + version delta
  const header = `<div style="background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:10px 14px;margin-bottom:14px">
    <div style="font-size:0.85rem;margin-bottom:6px"><strong>${escHtml(_adoptAppName)}</strong></div>
    <div style="font-size:0.78rem;color:var(--text2)">
      <code>${escHtml(data.from_version)}</code> → <code>${escHtml(data.to_version)}</code>
    </div>
  </div>`;

  // Diff classification banner
  let banner;
  if (kind === 'no_change') {
    banner = `<div style="font-size:0.82rem;color:var(--text2);padding:8px 12px;background:var(--bg2);border-radius:5px;margin-bottom:14px">
      No functional differences between versions. Adopt will just bump the version pointer.
    </div>`;
  } else if (kind === 'presentation_only') {
    banner = `<div style="font-size:0.82rem;color:var(--green);padding:8px 12px;background:var(--bg2);border-radius:5px;margin-bottom:14px">
      ✓ Presentation-only diff — safe to adopt with pointer-only rebind.
    </div>`;
  } else if (kind === 'structural') {
    const struct = (diff.structural_fields_touched || []).join(', ') || '(unspecified)';
    banner = `<div style="font-size:0.82rem;color:var(--red);padding:8px 12px;background:var(--bg2);border-left:3px solid var(--red);border-radius:5px;margin-bottom:14px">
      ⚠ <strong>Structural changes detected</strong>: ${escHtml(struct)}.
      Adopt v1 can't rebind across these — use the gallery install flow to re-install at the new version through Forge.
    </div>`;
  } else {
    banner = `<div style="font-size:0.82rem;color:var(--yellow);padding:8px 12px;background:var(--bg2);border-radius:5px;margin-bottom:14px">
      Unknown diff classification: ${escHtml(kind)}
    </div>`;
  }

  // Field lists
  const fieldBlock = (label, items, color) => {
    if (!items || !items.length) return '';
    return `<div style="margin-bottom:8px">
      <div style="font-size:0.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:${color};margin-bottom:4px">${escHtml(label)}</div>
      <ul style="margin:0;padding-left:18px;font-size:0.78rem;color:var(--text1)">
        ${items.map(f => `<li><code>${escHtml(f)}</code></li>`).join('')}
      </ul>
    </div>`;
  };
  const fieldsHtml = (kind !== 'no_change') ? `<div style="margin-bottom:14px">
    ${fieldBlock('Changed', diff.fields_changed, 'var(--blue)')}
    ${fieldBlock('Added', diff.fields_added, 'var(--green)')}
    ${fieldBlock('Removed', diff.fields_removed, 'var(--red)')}
  </div>` : '';

  // Action row
  const reasonInput = `<input id="adopt-reason" placeholder="manual_adopt" style="font-size:0.78rem;padding:5px 8px;border:1px solid var(--border);border-radius:4px;background:var(--bg1);color:var(--text1);width:180px">`;
  const adoptBtnDisabled = !data.safe_to_adopt;
  const adoptBtn = adoptBtnDisabled
    ? `<button class="btn btn-ghost" disabled title="Structural changes need a Forge rebuild" style="opacity:0.5">Adopt</button>`
    : `<button class="btn btn-green" onclick="performAdopt()">Adopt</button>`;

  const actions = `<div style="display:flex;align-items:center;justify-content:space-between;gap:12px;padding-top:10px;border-top:1px solid var(--border)">
    <div style="font-size:0.72rem;color:var(--text3)">
      <label for="adopt-reason">Reason:</label> ${reasonInput}
    </div>
    <div style="display:flex;gap:8px">
      <button class="btn btn-ghost" onclick="closeAdoptModal()">Cancel</button>
      ${adoptBtn}
    </div>
  </div>`;

  body.innerHTML = header + banner + fieldsHtml + actions;
}

async function performAdopt() {
  if (!_adoptBotId || !_adoptAppId || !_adoptPreviewData) return;
  if (!_adoptPreviewData.safe_to_adopt) return;
  const reason = (document.getElementById('adopt-reason')?.value || '').trim() || 'manual_adopt';
  const body = {
    target_version: _adoptPreviewData.to_version,
    reason,
  };
  try {
    const r = await fetch(`/api/applications/${encodeURIComponent(_adoptBotId)}/${encodeURIComponent(_adoptAppId)}/adopt`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) {
      toast('✗ ' + (data.error || `Adopt failed (HTTP ${r.status})`), 'err');
      return;
    }
    toast(`✓ Adopted ${escHtml(_adoptPreviewData.from_version)} → ${escHtml(_adoptPreviewData.to_version)}`, 'ok');
    closeAdoptModal();
    // Refresh the Apps list so the drift badge clears.
    if (typeof loadCapabilities === 'function') {
      loadCapabilities();
    }
  } catch (e) {
    toast('✗ Adopt failed: ' + e, 'err');
  }
}

function closeAdoptModal() {
  document.getElementById('adopt-modal')?.classList.remove('open');
  _adoptBotId = null;
  _adoptAppId = null;
  _adoptAppName = null;
  _adoptPreviewData = null;
}

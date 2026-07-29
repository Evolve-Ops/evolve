// ════════════════════════════════════════════════════════════════════════
// Page subtab: Recovery (+ Pod-state commit list)
//
// Catches the Phase 3h miss — Recovery is a subtab of page-backup per
// pageRedirectRegistry but lived 17K lines away from the Backup main
// cluster Phase 3h extracted; folding it in would have required a
// non-contiguous extraction with more error surface.
//
// Two surfaces share state:
//   - the Overview "recovery banner" (panic button + paused indicator)
//   - the dedicated Recovery page (full rollback UI + audit history)
//
// State:
//   _recoveryState        — last fetch from /api/recovery/state
//   _recoveryPollTimer    — setInterval handle (30s slow-cadence poll)
//   _podCommits           — cache of last-fetched repo commits
//
// Loaders + renderers:
//   recoveryRefresh()                — re-fetches state + repaints
//   _renderRecoveryBanner()          — Overview-page banner
//   _renderRecoveryPauseCard()       — paused-state explainer
//   _renderRecoveryHistory()         — history table
//   _renderRecoveryPauseLog()        — pause-log table
//   _populateRecoveryBotPicker()     — per-bot dropdown for workspace rollback
//   recoveryLoadPoints()             — per-bot rollback-point list
//   recoveryLoadWorkspaceCommits()   — per-bot workspace commits
//   recoveryPauseAll() / recoveryResumeAll() — panic button + resume
//   recoveryRollbackPreview() / recoveryRollbackGo() — workspace rollback
//   recoveryReverseRollback(id)      — undo
//   _startRecoveryPoll()             — slow-cadence poller (boot-wired
//                                       via DOMContentLoaded)
//
// Pod-state commit list (V2.4-2 — recovery's sibling surface):
//   podCommitsRefresh / _renderPodCommits  — recent repo commits + UI
//   _renderPodRollbackLog                  — pod-rollback history table
//
// Cross-file linkages (runtime free-variable lookup):
//   - api(), toast(), escHtml(), botLabel() — core/
//   - _connState — still inline in main script (the poller checks the
//                  connection state before polling)
// ════════════════════════════════════════════════════════════════════════


// ══════════════════════════════════════════════════════
// Recovery — panic button + per-bot rollback (sprint pillar B2.e)
// ══════════════════════════════════════════════════════
//
// Two surfaces share state:
//   - the Overview "recovery banner" (panic button + paused indicator)
//   - the dedicated Recovery page (full rollback UI + audit history)
//
// _recoveryPoll() runs on a slow cadence whenever the dashboard is open
// so the panic banner stays current even if another operator paused via CLI.

let _recoveryState = null;
let _recoveryPollTimer = null;

async function recoveryRefresh() {
  // Always render fresh state into both surfaces. Used by:
  //   - the recovery banner poll (light: just the state pill)
  //   - the Recovery page activation (full: banner + pause card + rollback
  //     bot picker + history + pause log)
  const data = await api('GET', '/api/recovery/status');
  if (!data || data.error) {
    _renderRecoveryBanner({ paused: false, error: data && data.error });
    return;
  }
  _recoveryState = data;
  _renderRecoveryBanner(data);
  // Render the Recovery-page-specific bits only if the page exists.
  if (document.getElementById('recovery-pause-state')) {
    _renderRecoveryPauseCard(data);
    _renderRecoveryHistory(data.recent_rollbacks || []);
    _renderRecoveryPauseLog(data.recent_pause_events || []);
    _populateRecoveryBotPicker(data.bot_ids || []);
    _renderPodRollbackLog();  // pod-state rollback audit log (V2.4-2)
  }
}

function _renderRecoveryBanner(d) {
  const banner = document.getElementById('recovery-banner');
  const msg    = document.getElementById('recovery-banner-msg');
  const pauseBtn = document.getElementById('panic-pause-btn');
  const resumeBtn = document.getElementById('panic-resume-btn');
  const seg = document.getElementById('ov-recovery-segment');
  const badge = document.getElementById('badge-recovery');
  if (!banner) return;
  const paused = !!(d && d.paused);
  banner.setAttribute('data-paused', paused ? 'true' : 'false');
  if (paused) {
    const ps = (d && d.pause_state) || {};
    const at = ps.paused_at ? new Date(ps.paused_at).toLocaleString() : '?';
    const by = ps.initiated_by || 'unknown';
    const reason = ps.reason || 'no reason given';
    if (msg) {
      msg.innerHTML = `<strong style="color:var(--red)">All bots paused.</strong> ` +
                      `Since ${escHtml(at)} (by ${escHtml(by)} — ${escHtml(reason)}).`;
    }
    if (pauseBtn) pauseBtn.style.display = 'none';
    if (resumeBtn) resumeBtn.style.display = '';
    if (seg) {
      seg.title = `Paused since ${at} by ${by} — ${reason}`;
      const lbl = seg.querySelector('.sys-loop-label');
      if (lbl) { lbl.textContent = 'Paused'; lbl.style.color = 'var(--red)'; }
    }
    if (badge) { badge.style.display = ''; badge.textContent = '!'; }
  } else {
    if (msg) msg.textContent = 'If something goes wrong, stop everything fast.';
    if (pauseBtn) pauseBtn.style.display = '';
    if (resumeBtn) resumeBtn.style.display = 'none';
    if (seg) {
      seg.title = '';
      const lbl = seg.querySelector('.sys-loop-label');
      if (lbl) { lbl.textContent = 'Recovery'; lbl.style.color = ''; }
    }
    if (badge) { badge.style.display = 'none'; }
  }
}

function _renderRecoveryPauseCard(d) {
  const wrap = document.getElementById('recovery-pause-state');
  if (!wrap) return;
  const paused = !!(d && d.paused);
  const pBtn = document.getElementById('recovery-pause-btn');
  const rBtn = document.getElementById('recovery-resume-btn');
  if (paused) {
    const ps = d.pause_state || {};
    wrap.innerHTML = `<div style="padding:8px 12px;background:rgba(220,38,38,0.14);border:1px solid rgba(220,38,38,0.4);border-radius:6px;font-size:0.82rem">
      <span class="badge badge-sm badge-crit">PAUSED</span>
      &nbsp;Since ${escHtml(new Date(ps.paused_at || '').toLocaleString())}
      &nbsp;·&nbsp;by ${escHtml(ps.initiated_by || '?')}
      &nbsp;·&nbsp;${escHtml(ps.reason || '')}
    </div>`;
    pBtn.style.display = 'none';
    rBtn.style.display = '';
  } else {
    wrap.innerHTML = `<div style="padding:8px 12px;background:rgba(74,222,128,0.10);border:1px solid rgba(74,222,128,0.3);border-radius:6px;font-size:0.82rem">
      <span class="badge badge-sm badge-ok">RUNNING</span>
      &nbsp;${escHtml(String(d && d.bot_count || 0))} bot(s) operational.
    </div>`;
    pBtn.style.display = '';
    rBtn.style.display = 'none';
  }
}

function _renderRecoveryHistory(hist) {
  const body = document.getElementById('recovery-history-body');
  if (!body) return;
  if (!hist || !hist.length) {
    body.innerHTML = '<div class="empty">No config rollbacks yet — they\'ll appear here after you roll back a bot\'s config using the tool above.</div>';
    return;
  }
  let html = '<div class="resp-table-wrap"><table class="resp-table"><thead><tr>' +
    '<th>When</th><th>Bot</th><th>Target</th><th>Status</th><th>Reversed?</th><th></th>' +
    '</tr></thead><tbody>';
  for (const h of hist) {
    const ok = h.ok ? '<span class="badge badge-sm badge-ok">OK</span>'
                    : '<span class="badge badge-sm badge-crit">FAIL</span>';
    const rev = h.reversed_by_rollback_id
      ? `<span class="badge badge-sm badge-neutral">↩ ${escHtml(h.reversed_by_rollback_id.slice(0,8))}</span>`
      : (h.reversed_from_rollback_id
        ? `<span class="badge badge-sm badge-neutral">↶ reverse of ${escHtml(h.reversed_from_rollback_id.slice(0,8))}</span>`
        : '—');
    const btn = (h.ok && !h.reversed_by_rollback_id && !h.reversed_from_rollback_id)
      ? `<button class="btn btn-ghost btn-sm" onclick="recoveryReverseRollback('${escHtml(h.rollback_id)}')">Reverse</button>`
      : '';
    const target = (h.target_commit || '').slice(0,12);
    html += `<tr>
      <td data-label="When">${escHtml((h.started_at || '').slice(0,19).replace('T',' '))}</td>
      <td data-label="Bot">${escHtml(botLabel(h.bot_id))}</td>
      <td data-label="Target"><code style="font-size:0.75rem">${escHtml(target)}</code></td>
      <td data-label="Status">${ok}</td>
      <td data-label="Reversed?">${rev}</td>
      <td data-label="Action">${btn}</td>
    </tr>`;
  }
  html += '</tbody></table></div>';
  body.innerHTML = html;
}

function _renderRecoveryPauseLog(events) {
  const body = document.getElementById('recovery-pause-log-body');
  if (!body) return;
  if (!events || !events.length) {
    body.innerHTML = '<div class="empty">No pause events recorded yet — pausing or resuming bots above will be logged here.</div>';
    return;
  }
  let html = '<div class="resp-table-wrap"><table class="resp-table"><thead><tr><th>When</th><th>Action</th><th>By</th><th>Status</th><th>Bots</th></tr></thead><tbody>';
  for (const ev of events) {
    const ok = ev.ok ? '<span class="badge badge-sm badge-ok">OK</span>'
                     : '<span class="badge badge-sm badge-crit">PARTIAL</span>';
    const bots = (ev.bots || []).map(b => {
      const c = b.ok ? 'badge-ok' : 'badge-crit';
      return `<span class="badge badge-sm ${c}">${escHtml(botLabel(b.bot_id))}</span>`;
    }).join(' ');
    html += `<tr>
      <td data-label="When">${escHtml((ev.timestamp || '').slice(0,19).replace('T',' '))}</td>
      <td data-label="Action">${escHtml(ev.action || '')}</td>
      <td data-label="By">${escHtml(ev.initiated_by || '?')}</td>
      <td data-label="Status">${ok}</td>
      <td data-label="Bots">${bots}</td>
    </tr>`;
  }
  html += '</tbody></table></div>';
  body.innerHTML = html;
}

function _populateRecoveryBotPicker(bots) {
  const sel = document.getElementById('recovery-bot-select');
  if (!sel) return;
  // Preserve existing selection if still valid
  const prev = sel.value;
  sel.innerHTML = '<option value="">— pick a bot —</option>' +
    bots.map(b => `<option value="${escHtml(b)}">${escHtml(botLabel(b))}</option>`).join('');
  if (prev && bots.includes(prev)) {
    sel.value = prev;
  } else {
    // Reset dependent picker if bot selection no longer valid
    const points = document.getElementById('recovery-point-select');
    if (points) points.innerHTML = '<option value="">— pick a bot first —</option>';
    const btn = document.getElementById('recovery-rollback-btn');
    if (btn) btn.disabled = true;
  }
}

async function recoveryLoadPoints() {
  const sel = document.getElementById('recovery-bot-select');
  const points = document.getElementById('recovery-point-select');
  const btn = document.getElementById('recovery-rollback-btn');
  const hintEl = document.getElementById('recovery-rollback-empty-hint');
  if (hintEl) hintEl.innerHTML = '';
  if (!sel || !points) return;
  const bot = sel.value;
  // Fire the workspace-commits load in parallel — it doesn't gate the
  // dropdown but should appear/disappear in lockstep with the picker.
  recoveryLoadWorkspaceCommits(bot);
  if (!bot) {
    points.innerHTML = '<option value="">— pick a bot first —</option>';
    if (btn) btn.disabled = true;
    return;
  }
  points.innerHTML = '<option>Loading…</option>';
  const data = await api('GET', `/api/recovery/rollback-points/${encodeURIComponent(bot)}?limit=14`);
  if (!data || !data.ok) {
    points.innerHTML = '<option value="">— could not load —</option>';
    if (btn) btn.disabled = true;
    if (hintEl) hintEl.innerHTML = `<span style="color:var(--red)">Server error: ${escHtml((data && data.error) || 'unknown')}</span>`;
    return;
  }
  const pts = (data.points || []).filter(p => p.has_openclaw_json);
  if (!pts.length) {
    // Diagnose why so the operator knows whether this is a misconfiguration
    // (URL not set, workspace not a git repo) or just "no backups yet".
    let label = '— no rollback points —';
    let hint = '';
    if (!data.backup_url_configured) {
      label = '— no rollback points: backup not configured —';
      // Direct deep-link to the guided onboarding modal — same flow as the
      // CTA on the Backup tab's per-bot card, just opened from one click
      // here instead of the operator having to navigate over there first.
      hint = `<div style="color:var(--text2);display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span style="flex:1;min-width:200px">No <code>backupRepoUrl</code> set for <strong>${escHtml(bot)}</strong>. Rollback needs a backup repo to read commits from.</span>
        <button class="btn btn-primary btn-sm" onclick="openOnboardModal('github',['${escHtml(bot)}'])">Set up GitHub backup →</button>
      </div>`;
    } else if (!data.workspace_is_git) {
      label = '— no rollback points: workspace not initialized —';
      hint = `<span style="color:var(--text2)">The workspace at <code>~/.openclaw/workspace/</code> for <strong>${escHtml(bot)}</strong> is not yet a git repo. Run a deploy or visit <a href="javascript:void(0)" onclick="nav(document.querySelector('[data-page=backup]'));setTimeout(()=>{const el=document.querySelector('#page-backup .subtab[data-subtab=&quot;cloud&quot;]');if(el)el.click();},60)">Backup → Cloud</a> to initialize.</span>`;
    } else {
      label = '— no rollback points: no backup commits yet —';
      hint = `<span style="color:var(--text2)">The nightly backup job hasn't recorded any commits for <strong>${escHtml(bot)}</strong> yet. Wait for the 2 AM run, or trigger a backup manually.</span>`;
    }
    points.innerHTML = `<option value="">${escHtml(label)}</option>`;
    if (btn) btn.disabled = true;
    if (hintEl) hintEl.innerHTML = hint;
    return;
  }
  points.innerHTML = '<option value="">— pick a date —</option>' +
    pts.map(p => `<option value="${escHtml(p.commit_sha)}" title="${escHtml(p.summary)}">${escHtml(p.commit_date_local)} (${escHtml(p.commit_sha.slice(0,8))})</option>`).join('');
  if (btn) btn.disabled = false;
}

// Load the bot's full workspace commit log (not just nightly backup
// commits) for the context panel under the rollback dropdown. Hidden
// when no bot picked or no commits.
async function recoveryLoadWorkspaceCommits(bot) {
  const wrap = document.getElementById('recovery-workspace-commits-wrap');
  const body = document.getElementById('recovery-workspace-commits-body');
  const botLabelEl = document.getElementById('recovery-workspace-commits-bot');
  if (!wrap || !body) return;
  if (!bot) {
    wrap.style.display = 'none';
    body.innerHTML = '';
    return;
  }
  wrap.style.display = '';
  if (botLabelEl) botLabelEl.textContent = bot;
  body.innerHTML = '<div class="loading"><span class="spinner"></span>Loading workspace commits…</div>';
  const data = await api(
    'GET',
    `/api/recovery/rollback-points/${encodeURIComponent(bot)}?limit=30&only_backup=false`
  );
  if (!data || !data.ok) {
    body.innerHTML = `<div class="subtle" style="color:var(--red)">Could not load workspace commits: ${escHtml((data && data.error) || 'unknown')}</div>`;
    return;
  }
  const pts = data.points || [];
  if (!pts.length) {
    // Same empty-state taxonomy as the rollback dropdown — keeps the
    // operator's mental model coherent.
    if (!data.backup_url_configured) {
      body.innerHTML = `<div class="subtle" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span style="flex:1;min-width:200px">No <code>backupRepoUrl</code> set — no commits yet.</span>
        <button class="btn btn-primary btn-sm" onclick="openOnboardModal('github',['${escHtml(bot)}'])">Set up GitHub backup →</button>
      </div>`;
    } else if (!data.workspace_is_git) {
      body.innerHTML = `<div class="empty">Workspace at <code>~/.openclaw/workspace/</code> is not yet a git repo. Run a deploy or trigger a backup to create one.</div>`;
    } else {
      body.innerHTML = `<div class="empty">No commits in this bot's workspace yet — run a deploy or trigger a backup to create one.</div>`;
    }
    return;
  }
  // Render compact rows. Mark which commits are nightly backup snapshots
  // (eligible rollback targets) vs intermediate work (accept-drift, etc.).
  const rows = pts.map(p => {
    const isBackup = p.is_backup_commit;
    const dot = isBackup
      ? '<span title="Nightly backup snapshot (eligible rollback target)" style="color:var(--green);font-weight:700">●</span>'
      : '<span title="Intermediate commit (not a rollback target)" style="color:var(--text3)">○</span>';
    const sha = (p.commit_sha || '').slice(0, 8);
    const when = escHtml(p.commit_date_local || p.commit_date || '?');
    const subj = escHtml(p.summary || '');
    return `<div style="display:flex;gap:8px;align-items:baseline;padding:3px 0;border-bottom:1px solid rgba(255,255,255,0.03)">
      <span style="flex-shrink:0;width:16px;text-align:center">${dot}</span>
      <code style="flex-shrink:0;font-size:0.72rem;color:var(--text3)">${escHtml(sha)}</code>
      <span style="flex-shrink:0;color:var(--text2);font-size:0.74rem;min-width:170px">${when}</span>
      <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${subj}">${subj}</span>
    </div>`;
  }).join('');
  // Legend up top so the dot color is decodable without hovering.
  const legend = `<div style="display:flex;gap:14px;align-items:center;margin-bottom:6px;color:var(--text3);font-size:0.72rem">
    <span><span style="color:var(--green);font-weight:700">●</span> nightly backup (rollback target)</span>
    <span><span style="color:var(--text3)">○</span> intermediate commit</span>
  </div>`;
  body.innerHTML = legend + rows;
}

async function recoveryPauseAll() {
  const ok = await confirmModal({
    body: (
      'Pause ALL bots in the pod?\n\n' +
      'Every bot gateway will be stopped. Bot data is preserved.\n' +
      'You can resume from this same page. Continue?'
    ),
    danger: true,
  });
  if (!ok) return;
  const reason = prompt('Reason (recorded in audit log):', 'operator pause') || 'operator pause';
  const btn = document.getElementById('recovery-pause-btn') || document.getElementById('panic-pause-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Pausing…'; }
  const t0 = Date.now();
  const data = await fetch('/api/recovery/pause-all', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason, initiated_by: 'web' }),
  }).then(r => r.json()).catch(e => ({ ok: false, error: String(e) }));
  const elapsed = Date.now() - t0;
  if (btn) { btn.disabled = false; btn.textContent = 'Pause all bots'; }
  _showRecoveryActionResult('recovery-pause-result', data, `Pause-all in ${elapsed}ms`);
  await recoveryRefresh();
  if (data && data.ok) {
    toast('All bots paused', 'ok');
  } else {
    toast('Pause-all partial — see Maintenance → Recovery', 'err');
  }
}

async function recoveryResumeAll() {
  const ok = await confirmModal({ body: 'Resume ALL bots in the pod? Every gateway will be re-enabled.', danger: true });
  if (!ok) return;
  const btn = document.getElementById('recovery-resume-btn') || document.getElementById('panic-resume-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Resuming…'; }
  const t0 = Date.now();
  const data = await fetch('/api/recovery/resume-all', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ initiated_by: 'web' }),
  }).then(r => r.json()).catch(e => ({ ok: false, error: String(e) }));
  const elapsed = Date.now() - t0;
  if (btn) { btn.disabled = false; btn.textContent = 'Resume all bots'; }
  _showRecoveryActionResult('recovery-pause-result', data, `Resume-all in ${elapsed}ms`);
  await recoveryRefresh();
  if (data && data.ok) toast('All bots resumed', 'ok');
  else toast('Resume-all partial — see Maintenance → Recovery', 'err');
}

// ══════════════════════════════════════════════════════
// Circuit breakers (Phase 4b)
// Modal-driven UI for trip / reset on per-bot or pod-wide scope.
// Backend: /api/breakers, /api/breakers/trip, /api/breakers/reset.
// State source: /api/status returns active_breakers per-bot and a
// top-level pod_breakers array.
// ══════════════════════════════════════════════════════

let _breakerModalScope = null;   // bot_id string or "pod"

function openBreakerModal(scope, isPod) {
  _breakerModalScope = isPod ? 'pod' : scope;
  const modal = document.getElementById('breaker-modal');
  const title = document.getElementById('breaker-modal-title');
  const scopeNote = document.getElementById('breaker-modal-scope-note');
  if (!modal) return;
  if (_breakerModalScope === 'pod') {
    title.textContent = 'Pod-wide circuit breakers';
    scopeNote.textContent = 'Affects every bot in the pod.';
  } else {
    title.textContent = `Circuit breakers — ${_breakerModalScope}`;
    scopeNote.textContent = '';
  }
  // Reset form state.
  const reasonEl = document.getElementById('breaker-modal-reason');
  if (reasonEl) reasonEl.value = '';
  const durEl = document.getElementById('breaker-modal-duration');
  if (durEl) durEl.value = '24h';
  const errEl = document.getElementById('breaker-modal-error');
  if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }
  renderBreakerModalActive();
  // Use the .open class pattern that the rest of the .modal-overlay
  // CSS expects (`.modal-overlay.open { display: flex }`). Setting
  // inline style.display='flex' mostly works, but if the inline style
  // ever gets stuck on a stale value the modal becomes invisible-yet-
  // open and the next click appears inert. Also clear any leftover
  // inline display so the class is the only thing driving visibility.
  modal.classList.add('open');
  modal.style.display = '';
}

function closeBreakerModal() {
  const modal = document.getElementById('breaker-modal');
  if (modal) {
    modal.classList.remove('open');
    modal.style.display = '';
  }
  _breakerModalScope = null;
}

function renderBreakerModalActive() {
  const box = document.getElementById('breaker-modal-active');
  if (!box) return;
  const scope = _breakerModalScope;
  // Read active breakers from the cached status payload — same data the
  // tiles render from. No fresh fetch needed; the dashboard polls
  // /api/status every 5s already.
  let trips = [];
  if (scope === 'pod') {
    trips = (_statusData?.pod_breakers || []).slice();
  } else {
    const bot = (_statusData?.bots || {})[scope] || {};
    trips = (bot.active_breakers || []).slice();
  }
  if (!trips.length) {
    box.innerHTML = `<div style="font-size:0.78rem;color:var(--text3);padding:8px 0">No active breakers.</div>`;
    return;
  }
  box.innerHTML = `
    <div style="font-size:0.8rem;color:var(--text2);margin-bottom:8px;font-weight:600">Currently tripped</div>
    ${trips.map(t => {
      const isFull = t.type === 'full';
      const color = isFull ? 'var(--red,#f87171)' : 'var(--yellow,#fbbf24)';
      const bg = isFull ? 'rgba(248,113,113,0.10)' : 'rgba(251,191,36,0.10)';
      const expires = t.expires_at
        ? `expires ${_humanTs(t.expires_at)}`
        : '<em>indefinite</em>';
      const tripIdShort = (t.trip_id || '').slice(0, 8);
      // Diagnosis section — populated asynchronously by Phase 5c's
      // audit-of-cause generator. Until that lands, audit_summary and
      // audit_recommendation are null and this block stays collapsed.
      // When present, render both fields with subtle separation so the
      // operator can read "what happened" and "what to do" without
      // leaving the modal.
      const _hasDiag = t.audit_summary || t.audit_recommendation;
      const diagHtml = _hasDiag
        ? `<div style="margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.08)">
            <div style="font-size:0.7rem;color:var(--text3);margin-bottom:4px;font-weight:600;letter-spacing:0.04em;text-transform:uppercase">📋 Diagnosis</div>
            ${t.audit_summary ? `<div style="font-size:0.76rem;color:var(--text);margin-bottom:6px;line-height:1.4">${escHtml(t.audit_summary)}</div>` : ''}
            ${t.audit_recommendation ? `<div style="font-size:0.74rem;color:var(--text2);font-style:italic;line-height:1.4"><span style="color:var(--text3);font-style:normal">Recommendation: </span>${escHtml(t.audit_recommendation)}</div>` : ''}
           </div>`
        : '';
      // L2 trips get a prominent green "Reactivate" CTA (operator
      // mental model: "turn it back on", not "reset"). L1 trips —
      // which only block background activity, not user chat — get
      // the lighter "Resume" label.
      const _resetLabel = t.type === 'full' ? '🔄 Reactivate' : '▶ Resume';
      const _resetStyle = t.type === 'full'
        ? 'font-size:0.78rem;padding:5px 14px;background:rgba(61,217,132,0.18);border:1px solid var(--green,#3dd984);color:var(--green,#3dd984);font-weight:600'
        : 'font-size:0.7rem;padding:3px 10px';
      return `<div style="background:${bg};border:1px solid ${color};border-radius:8px;padding:10px;margin-bottom:8px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
          <span style="font-weight:600;color:${color}">${escHtml(t.type === 'full' ? 'Full halt (L2)' : 'Cost breaker (L1)')}</span>
          <button class="btn btn-ghost btn-sm" style="${_resetStyle}" onclick="submitBreakerReset('${escHtml(t.type)}')">${_resetLabel}</button>
        </div>
        <div style="font-size:0.72rem;color:var(--text2);margin-bottom:2px">${expires} · by ${escHtml(t.initiated_by || '?')}</div>
        ${t.reason ? `<div style="font-size:0.74rem;color:var(--text)">${escHtml(t.reason)}</div>` : ''}
        <div style="font-size:0.66rem;color:var(--text3);margin-top:4px">trip id: ${escHtml(tripIdShort)}</div>
        ${diagHtml}
      </div>`;
    }).join('')}
  `;
}

function _humanTs(iso) {
  if (!iso) return '?';
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString();
  } catch { return iso; }
}

function _breakerError(msg) {
  const el = document.getElementById('breaker-modal-error');
  if (!el) return;
  el.textContent = msg;
  el.style.display = 'block';
}

async function submitBreakerTrip(breakerType) {
  const scope = _breakerModalScope;
  if (!scope) return;
  // Reason is optional — server defaults to "operator trip" in the
  // audit log when omitted. Friction-free for the common case;
  // operators who DO want to annotate the trip still can.
  const reason = (document.getElementById('breaker-modal-reason')?.value || '').trim()
    || 'operator trip via web admin';
  const duration = document.getElementById('breaker-modal-duration')?.value || '24h';
  // L2 / pod-wide trips get a destructive confirm gate. L1 per-bot trips
  // skip confirm — they\'re recoverable and user chat keeps working.
  const isFull = breakerType === 'full';
  const isPod = scope === 'pod';
  if (isFull || isPod) {
    const scopeDesc = isPod ? 'EVERY bot in the pod' : `bot "${scope}"`;
    const effect = isFull
      ? `take down ${scopeDesc} (gateway offline)`
      : `block background activity on ${scopeDesc}`;
    if (!await confirmModal({ body: `This will ${effect}. Continue?`, danger: true })) return;
  }
  const errEl = document.getElementById('breaker-modal-error');
  if (errEl) errEl.style.display = 'none';
  const data = await fetch('/api/breakers/trip', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      scope, type: breakerType, duration, reason, initiated_by: 'web',
    }),
  }).then(r => r.json()).catch(e => ({ ok: false, error: String(e) }));
  // Three outcomes — distinguish so the operator never sees "Trip failed"
  // when the breaker is in fact persisted on disk:
  //   - full success:    data.ok === true (state written + enforce ok)
  //   - partial success: data.ok === false BUT data.trip is present
  //                      (HTTP 207 — state persisted, enforce/launchctl
  //                      failed; re-clicking would create a duplicate
  //                      trip_id + a spurious "retrip" audit row)
  //   - full failure:    data.ok === false AND data.trip is null/missing
  //                      (HTTP 400/500 — bad input or store crash; no
  //                      state was written, safe to retry)
  if (data && data.ok) {
    toast(`Breaker tripped: ${scope}/${breakerType}`, 'ok');
    closeBreakerModal();
    await loadStatus();
  } else if (data && data.trip) {
    toast(
      `Breaker tripped (enforce failed) — see admin log for ${scope}/${breakerType}`,
      'warn',
    );
    closeBreakerModal();
    await loadStatus();
  } else {
    _breakerError(data?.error || 'Trip failed — see admin server log');
  }
}

async function submitBreakerReset(breakerType) {
  const scope = _breakerModalScope;
  if (!scope) return;
  // Reactivating from L2 re-exposes the bot — confirm. Cost-breaker
  // reactivation is silent (just un-blocks background activity).
  if (breakerType === 'full' || scope === 'pod') {
    const scopeDesc = scope === 'pod' ? 'every bot' : `bot "${scope}"`;
    if (!await confirmModal({ body: `Reactivate ${scopeDesc}? This brings ${scopeDesc === 'every bot' ? 'them' : 'it'} back online.`, danger: true })) return;
  }
  const data = await fetch('/api/breakers/reset', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      scope, type: breakerType, initiated_by: 'web',
    }),
  }).then(r => r.json()).catch(e => ({ ok: false, error: String(e) }));
  // Same partial-success handling as trip: state may be cleared on disk
  // even when enforce_reset (launchctl bootstrap) didn't succeed.
  if (data && data.ok) {
    toast(`Reactivated: ${scope}/${breakerType}`, 'ok');
    await loadStatus();
    renderBreakerModalActive();
  } else if (data && data.reset) {
    toast(
      `Breaker cleared — gateway bring-up may need a manual restart for ${scope}/${breakerType}`,
      'warn',
    );
    await loadStatus();
    renderBreakerModalActive();
  } else {
    _breakerError(data?.error || 'Reactivation failed — see admin server log');
  }
}

// Tile-level "Reactivate" entry point — bypasses the modal for the
// common case. Shown by renderPodNode whenever ANY breaker is active
// on the bot (L1, L2, or both). Reads the bot's active_breakers from
// the cached /api/status payload and clears each one in turn.
// /api/breakers/reset on an absent breaker is a no-op (returns
// was_tripped=false), so this is safe even if state changes between
// the cache read and the actual reset calls.
async function reactivateBotFromTile(botId) {
  if (!botId) return;
  const bot = (_statusData?.bots || {})[botId] || {};
  const podTypes = new Set((_statusData?.pod_breakers || []).map(b => b.type));
  // Per-bot types take priority; pod-wide types apply unless the
  // operator chooses to reactivate just this bot (we surface that
  // via the confirm dialog).
  const perBotTypes = new Set((bot.active_breakers || []).map(b => b.type));
  // If the only active breaker is pod-wide, the operator should reset
  // pod-wide rather than per-bot. Defer to the modal in that case.
  if (perBotTypes.size === 0 && podTypes.size > 0) {
    if (await confirmModal(`The active breaker on "${botLabel(botId)}" is pod-wide. Open the breaker panel to reset it for the whole pod?`)) {
      openBreakerModal('pod', true);
    }
    return;
  }
  const types = Array.from(perBotTypes);
  if (types.length === 0) {
    toast(`No active breakers on ${botLabel(botId)}`, 'ok');
    await loadStatus();
    return;
  }
  // Confirm — copy adapts to which breaker(s) are about to clear.
  const hasL2 = perBotTypes.has('full');
  const confirmMsg = hasL2
    ? `Reactivate "${botId}"? This brings the gateway back online and clears the breaker(s).`
    : `Clear the cost breaker on "${botId}"? Background activity will resume.`;
  if (!await confirmModal({ body: confirmMsg, danger: true })) return;

  // Clear each active type sequentially. Order: full first (gateway
  // up before any subsequent calls might need it), cost second.
  const ordered = types.sort((a, b) => a === 'full' ? -1 : 1);
  const results = [];
  for (const t of ordered) {
    const data = await fetch('/api/breakers/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope: botId, type: t, initiated_by: 'web' }),
    }).then(r => r.json()).catch(e => ({ ok: false, error: String(e) }));
    results.push({ type: t, data });
  }
  // Summarize across the (1 or 2) reset calls.
  const allOk = results.every(r => r.data && r.data.ok);
  const anyPartial = results.some(r => r.data && !r.data.ok && r.data.reset);
  if (allOk) {
    toast(`Reactivated ${botLabel(botId)}`, 'ok');
  } else if (anyPartial) {
    toast(`${botLabel(botId)} breakers cleared — gateway bring-up may need a manual restart`, 'warn');
  } else {
    const firstErr = results.find(r => !(r.data?.ok))?.data?.error || 'see admin log';
    toast(`Reactivation failed for ${botLabel(botId)}: ${firstErr}`, 'err');
  }
  await loadStatus();
}

async function recoveryRollbackPreview() {
  const bot = (document.getElementById('recovery-bot-select') || {}).value || '';
  const target = (document.getElementById('recovery-point-select') || {}).value || '';
  if (!bot || !target) { toast('Pick a bot and a rollback point first.', 'err'); return; }
  const data = await fetch('/api/recovery/rollback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bot_id: bot, target, initiated_by: 'web', dry_run: true }),
  }).then(r => r.json()).catch(e => ({ ok: false, error: String(e) }));
  _showRecoveryActionResult('recovery-rollback-result', data, 'Preview (dry-run)');
}

async function recoveryRollbackGo() {
  const bot = (document.getElementById('recovery-bot-select') || {}).value || '';
  const target = (document.getElementById('recovery-point-select') || {}).value || '';
  if (!bot || !target) { toast('Pick a bot and a rollback point first.', 'err'); return; }
  const ok = await confirmModal({
    body: (
      `Roll back ${bot}'s config to ${target.slice(0,8)}?\n\n` +
      `The bot's openclaw.json will be replaced with the version from that commit, ` +
      `and the gateway will be restarted.\n\n` +
      `The current config will be snapshotted first — you can reverse this rollback later.`
    ),
    danger: true,
  });
  if (!ok) return;
  const btn = document.getElementById('recovery-rollback-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Rolling back…'; }
  const data = await fetch('/api/recovery/rollback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bot_id: bot, target, initiated_by: 'web' }),
  }).then(r => r.json()).catch(e => ({ ok: false, error: String(e) }));
  if (btn) { btn.disabled = false; btn.textContent = 'Roll back'; }
  _showRecoveryActionResult('recovery-rollback-result', data, 'Rollback');
  await recoveryRefresh();
  if (data && data.ok) toast(`Rolled ${botLabel(bot)} back to ${target.slice(0,8)}`, 'ok');
  else toast('Rollback failed — see Maintenance → Recovery', 'err');
}

async function recoveryReverseRollback(rollbackId) {
  if (!rollbackId) return;
  const ok = await confirmModal({ body: `Undo rollback ${rollbackId.slice(0,8)}?\n\nThis restores the pre-rollback config and restarts the gateway.`, danger: true });
  if (!ok) return;
  const data = await fetch('/api/recovery/reverse-rollback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rollback_id: rollbackId, initiated_by: 'web' }),
  }).then(r => r.json()).catch(e => ({ ok: false, error: String(e) }));
  _showRecoveryActionResult('recovery-rollback-result', data, 'Reverse-rollback');
  await recoveryRefresh();
  if (data && data.ok) toast('Rollback reversed', 'ok');
  else toast('Reverse failed — see Maintenance → Recovery', 'err');
}

function _showRecoveryActionResult(elementId, data, header) {
  const el = document.getElementById(elementId);
  if (!el) return;
  if (!data) { el.innerHTML = ''; return; }
  const ok = !!data.ok;
  const color = ok ? 'rgba(74,222,128,0.10)' : 'rgba(220,38,38,0.10)';
  const border = ok ? 'rgba(74,222,128,0.35)' : 'rgba(220,38,38,0.4)';
  let body = '';
  if (data.message) body += `<div>${escHtml(data.message)}</div>`;
  if (data.error)   body += `<div style="color:var(--red)">${escHtml(data.error)}</div>`;
  if (Array.isArray(data.per_bot)) {
    body += '<ul style="margin:6px 0 0;padding-left:20px;font-size:0.78rem">';
    for (const r of data.per_bot) {
      const mark = r.ok ? '✓' : '✗';
      const extra = r.stderr ? ` — <code>${escHtml(r.stderr)}</code>` : (r.skip_reason ? ` — ${escHtml(r.skip_reason)}` : '');
      body += `<li>${mark} <strong>${escHtml(botLabel(r.bot_id))}</strong> (rc=${r.rc==null?'?':r.rc}, ${r.elapsed_ms}ms)${extra}</li>`;
    }
    body += '</ul>';
  }
  if (data.gateway_restart_msg) {
    body += `<div style="margin-top:6px;font-size:0.78rem;color:var(--text2)">gateway-restart: ${escHtml(data.gateway_restart_msg)}</div>`;
  }
  el.innerHTML = `<div style="padding:10px 12px;background:${color};border:1px solid ${border};border-radius:6px;font-size:0.82rem">
    <div style="font-weight:600;margin-bottom:4px">${escHtml(header)} — ${ok ? 'ok' : 'failed'}${data.dry_run ? ' (dry-run)' : ''}</div>
    ${body}
  </div>`;
}

// Poll recovery state quietly so the banner stays current.
// We tie it to the dashboard's existing connection state.
function _startRecoveryPoll() {
  if (_recoveryPollTimer) return;
  // Initial refresh
  recoveryRefresh();
  // Slow cadence — every 30 seconds. The banner mostly stays in sync via
  // direct refresh after panic/resume calls; this poll catches the
  // operator-paused-via-CLI case.
  _recoveryPollTimer = setInterval(() => {
    if (_connState === 'ok') recoveryRefresh();
  }, 30000);
}
// Hook into DOM ready or immediately if already loaded.
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _startRecoveryPoll);
} else {
  _startRecoveryPoll();
}


// ── Pod-state commit list (V2.4-2) ──────────────────────────────────────────
// Shows recent commits from /Users/Shared/evolve-repo with a one-click
// rollback button.  The confirm_token from the API response gates the actual
// rollback (single-use, 30-second TTL, checked server-side).

let _podCommits = [];  // cache of last-fetched commits

async function podCommitsRefresh() {
  const body = document.getElementById('pod-commits-body');
  const status = document.getElementById('pod-commits-status');
  if (!body) return;
  body.innerHTML = '<div class="loading"><span class="spinner"></span>Loading commits…</div>';
  if (status) status.textContent = '';
  const data = await api('GET', '/api/recovery/recent-commits?days=7&limit=50');
  if (!data || !data.ok) {
    const msg = (data && data.error) || 'failed to load commits';
    body.innerHTML = `<div class="subtle" style="color:var(--red)">${escHtml(msg)}</div>`;
    return;
  }
  _podCommits = data.commits || [];
  if (status) status.textContent = `${_podCommits.length} commit(s) in the last 7 days`;
  _renderPodCommits(_podCommits);
  _renderPodRollbackLog();
}

function _renderPodCommits(commits) {
  const body = document.getElementById('pod-commits-body');
  if (!body) return;
  if (!commits || !commits.length) {
    body.innerHTML = '<div class="empty">No commits in the last 7 days — the deploy checkout may be offline or stale. <button class="btn btn-ghost btn-sm" onclick="podCommitsRefresh()">Refresh →</button></div>';
    return;
  }
  let html = '';
  for (const c of commits) {
    const ts = c.timestamp ? new Date(c.timestamp).toLocaleString() : '?';
    const hi = c.high_impact_paths && c.high_impact_paths.length
      ? `<span class="pod-commit-hi" title="High-impact paths: ${escHtml(c.high_impact_paths.join(', '))}">&#9888; high-impact</span>`
      : '';
    const safePill = c.rollback_safe
      ? ''
      : '<span class="badge badge-sm badge-crit" style="margin-left:4px" title="This rollback may lose local-only commits">unsafe</span>';
    const btnId = `pod-rb-btn-${escHtml(c.short_sha)}`;
    html += `<div class="pod-commit-row">
      <span class="pod-commit-sha" title="${escHtml(c.sha)}">${escHtml(c.short_sha)}</span>
      <span class="pod-commit-subject" title="${escHtml(c.subject)}">${escHtml(c.subject)}${hi}${safePill}</span>
      <span class="pod-commit-meta">${escHtml(c.author)} &middot; ${escHtml(ts)} &middot; ${escHtml(String(c.files_changed))} file(s)</span>
      <button id="${btnId}" class="btn btn-danger btn-sm" style="flex-shrink:0"
              onclick="podRollbackConfirm(${JSON.stringify(c.sha)}, ${JSON.stringify(c.short_sha)}, ${JSON.stringify(c.subject)}, ${JSON.stringify(c.rollback_size_estimate)}, ${JSON.stringify(c.confirm_token)})">
        Rollback to here
      </button>
    </div>`;
  }
  body.innerHTML = html;
}

async function podRollbackConfirm(sha, shortSha, subject, sizeEst, confirmToken) {
  // Show confirmation modal
  const existing = document.getElementById('pod-rollback-modal-overlay');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'pod-rollback-modal-overlay';
  overlay.className = 'pod-rollback-modal-overlay';
  overlay.innerHTML = `
    <div class="pod-rollback-modal" role="dialog" aria-modal="true" aria-labelledby="pod-rb-modal-title">
      <h3 id="pod-rb-modal-title">Rollback pod state to ${escHtml(shortSha)}?</h3>
      <div class="pod-rollback-modal-body">
        <strong>Commit:</strong> ${escHtml(subject)}<br>
        <strong>Change estimate:</strong> ${escHtml(sizeEst || 'unknown')}<br>
        <br>
        This will run <code>git reset --hard ${escHtml(shortSha)}</code> on the deploy
        checkout and restart the admin-UI daemon. All bots will continue running but will
        pick up the reverted code on their next restart. The page will reload automatically.
        <br><br>
        <strong style="color:var(--red)">This is pod-wide and immediate. Are you sure?</strong>
      </div>
      <div id="pod-rb-modal-result"></div>
      <div class="pod-rollback-modal-actions">
        <button class="btn btn-ghost" onclick="document.getElementById('pod-rollback-modal-overlay').remove()">Cancel</button>
        <button class="btn btn-danger" id="pod-rb-modal-confirm-btn"
                onclick="podRollbackExecute(${JSON.stringify(sha)}, ${JSON.stringify(shortSha)}, ${JSON.stringify(confirmToken)}, this)">
          Yes, rollback to ${escHtml(shortSha)}
        </button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  // Dismiss on backdrop click
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.remove();
  });
}

async function podRollbackExecute(sha, shortSha, confirmToken, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Rolling back…'; }
  const resultEl = document.getElementById('pod-rb-modal-result');

  const data = await fetch('/api/recovery/rollback-pod', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      commit_sha: sha,
      confirm_token: confirmToken,
      initiated_by: 'web',
    }),
  }).then(r => r.json()).catch(e => ({ ok: false, error: String(e) }));

  if (data && data.ok) {
    if (resultEl) resultEl.innerHTML = `<div style="padding:8px 12px;background:rgba(74,222,128,0.10);border:1px solid rgba(74,222,128,0.3);border-radius:6px;font-size:0.82rem;margin-bottom:12px">
      <strong style="color:var(--green)">Rollback complete.</strong>
      ${escHtml(data.message || '')}
      ${data.daemon_restart_ok === false ? '<br><span style="color:var(--orange,#f97316)">Admin-UI restart failed — reload the page manually.</span>' : ''}
    </div>`;
    toast(`Rolled back to ${shortSha}`, 'ok');
    // The admin-ui daemon is restarting; wait a moment then reload
    setTimeout(() => { window.location.reload(); }, 3000);
  } else {
    const errMsg = (data && data.error) || (data && data.message) || 'unknown error';
    if (resultEl) resultEl.innerHTML = `<div style="padding:8px 12px;background:rgba(220,38,38,0.10);border:1px solid rgba(220,38,38,0.4);border-radius:6px;font-size:0.82rem;margin-bottom:12px;color:var(--red)">
      <strong>Rollback failed:</strong> ${escHtml(errMsg)}
    </div>`;
    if (btn) { btn.disabled = false; btn.textContent = `Yes, rollback to ${shortSha}`; }
    toast('Pod rollback failed — see modal for details', 'err');
  }
}

async function _renderPodRollbackLog() {
  const body = document.getElementById('recovery-pod-rollback-log-body');
  if (!body) return;
  const data = await api('GET', '/api/recovery/pod-rollback-log?limit=20');
  if (!data || !data.ok || !data.log || !data.log.length) {
    body.innerHTML = '<div class="empty">No pod-state rollbacks recorded yet — they\'ll appear here after a pod-state rollback from the commit list above.</div>';
    return;
  }
  let html = '<div class="resp-table-wrap"><table class="resp-table"><thead><tr><th>When</th><th>Target SHA</th><th>Files</th><th>By</th><th>Status</th><th>Daemon restart</th></tr></thead><tbody>';
  for (const e of data.log) {
    const ok = e.ok
      ? '<span class="badge badge-sm badge-ok">OK</span>'
      : '<span class="badge badge-sm badge-crit">FAIL</span>';
    const dr = e.daemon_restart_ok === true
      ? '<span class="badge badge-sm badge-ok">restarted</span>'
      : (e.daemon_restart_ok === false
        ? '<span class="badge badge-sm badge-crit">failed</span>'
        : '<span class="badge badge-sm badge-neutral">—</span>');
    html += `<tr>
      <td data-label="When">${escHtml((e.started_at || '').slice(0,19).replace('T',' '))}</td>
      <td data-label="Target SHA"><code style="font-size:0.75rem">${escHtml((e.target_sha || '').slice(0,8))}</code></td>
      <td data-label="Files">${escHtml(String(e.files_changed || 0))}</td>
      <td data-label="By">${escHtml(e.initiated_by || '?')}</td>
      <td data-label="Status">${ok}${e.dry_run ? ' <span class="badge badge-sm badge-neutral">dry-run</span>' : ''}</td>
      <td data-label="Daemon restart">${dr}</td>
    </tr>`;
  }
  html += '</tbody></table></div>';
  body.innerHTML = html;
}


// ── Mobile sidebar drawer — Phase 0 §4.3.b ──────────────────────────────
// Opens/closes the sidebar-as-drawer below 980px. Wires:

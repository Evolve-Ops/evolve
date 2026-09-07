// ════════════════════════════════════════════════════════════════════════
// Page: Backup (main cluster — config + status + data overview + cloud)
//
// Phase 1b promoted Backup to its own top-level page. Three subtabs
// ship here:
//   - Status   — read-only roll-up of /api/backup/cloud/status,
//                rendered as a compact per-bot summary (the Status
//                strip + Status summary). Reuses the same endpoint
//                Cloud uses, so swapping subtabs is instant.
//   - Cloud    — full configure-and-test surface; per-bot URL editor,
//                key generation, distribute-key flow, accept-drift,
//                workspace init, push test, backup-now action.
//   - Data     — data-overview tab (Apps tier, Pod tier with privacy
//                options + pod-paths editor).
//
// State (3 lets at the top of the file):
//   _backupCfgData   — cached /api/backup/cloud/status response
//   _backupDataState — cached /api/backup/data/overview response
//   const _BACKUP_TIERS / _BACKUP_PRIVACY_OPTIONS — lookup tables for
//     the data-overview tier dropdowns + privacy column.
//
// Loaders dispatched via onPageActivate('backup') + subtab activators:
//   loadBackupStatusSignalsStrip()  — Status subtab signals strip
//   loadBackupStatusSummary()       — Status subtab summary card
//   loadBackupDataOverview()        — Data subtab (Apps + Pod tiers)
//   loadBackupConfig()              — Cloud subtab (per-bot config grid)
//   loadBackupPreflight(botId)      — per-bot preflight (before backup-now)
//
// Actions (called from inline onclick attributes):
//   backupNowFromDiagnostic / backupNow / backupNowAll
//   backupSaveUrl / backupGenerateKey / backupCopyKey
//   backupAcceptDrift / backupInitWorkspace
//   backupDistributeKey / backupRunPushTest
//   backupDataSetTier / backupDataSetBotDefault / backupDataSetPodDefault
//   backupDataAddPodPath / backupDataRemovePodPath /
//   backupDataUpdatePodPathPrivacy
//
// Out of scope (separate clusters, future phases):
//   Recovery (recoveryRefresh + _recovery* + recoveryRollback* — line
//     ~52848 pre-Phase-3h). Recovery is a subtab of page-backup per
//     pageRedirectRegistry but lives 17K lines away from this cluster;
//     a future "backup-recovery" phase folds it in.
// ════════════════════════════════════════════════════════════════════════

// ── Backup config ──────────────────────────────────────────────────────────────

let _backupCfgData = null;

// Phase 1b: Backup is its own top-level page now. The Status subtab is a
// read-only roll-up that fires the same /api/backup/cloud/status endpoint
// the Cloud tab uses, rendered as a compact per-bot summary. Cloud + Recovery
// keep their existing loaders (loadBackupConfig / recoveryRefresh).
function _backupToggleTradeoffs(event) {
  if (event && event.preventDefault) event.preventDefault();
  const panel = document.getElementById('backup-tradeoffs-panel');
  if (!panel) return;
  panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}

// Mirrors the Security page's audit-signals strip — surfaces any
// firing Signal in the ``backup`` category (backup_signal,
// backup_audit_signal, local_backup_signal) so a backup-related
// alert (e.g. "GitHub PAT missing") shows on the Backup → Status page
// instead of only being visible on Alerts. Click a chip → Alerts page
// where the operator can snooze/dismiss/resolve.
async function loadBackupStatusSignalsStrip() {
  const strip = document.getElementById('backup-status-signals-strip');
  if (!strip) return;
  try {
    const d = await api('GET', '/api/signals?category=backup&state=firing&limit=50');
    const sigs = (d && d.signals) || [];
    if (!sigs.length) {
      strip.style.display = 'none';
      strip.innerHTML = '';
      return;
    }
    // Collapse duplicates by title — the same finding often fires on
    // multiple bots, and 8 near-identical chips are unreadable.
    const groups = new Map();
    for (const s of sigs) {
      const key = s.title || '(untitled)';
      const g = groups.get(key);
      if (!g) {
        groups.set(key, { title: key, severity: s.severity, last: s.last_observed_at, count: 1, body: s.body || '' });
      } else {
        g.count += 1;
        if ((s.last_observed_at || 0) > (g.last || 0)) g.last = s.last_observed_at;
        const order = { alert: 3, warn: 2, info: 1 };
        if ((order[s.severity] || 0) > (order[g.severity] || 0)) g.severity = s.severity;
      }
    }
    const sevOrder = { alert: 3, warn: 2, info: 1 };
    const grouped = [...groups.values()].sort((a, b) =>
      (sevOrder[b.severity] || 0) - (sevOrder[a.severity] || 0) || (b.last || 0) - (a.last || 0)
    );
    const counts = sigs.reduce((acc, s) => { acc[s.severity] = (acc[s.severity] || 0) + 1; return acc; }, {});
    const sevSummary = ['alert', 'warn', 'info']
      .filter(k => counts[k])
      .map(k => `${counts[k]} ${k}`)
      .join(' · ');
    // Alerts live under Reports → Alerts; pre-set the backup category tab
    // before the lane loads so the operator lands on exactly the rows they
    // were just looking at, not the firehose. ``_alActiveCategory`` is a
    // module-level let in this file (line ~52090), and _alLoadLane reads
    // it on every render — so writing it before the subtab click is
    // honored on the first render with no flash.
    const jumpJs = "document.querySelector('.nav-item[data-page=\\'reports\\']').click(); setTimeout(()=>{ _alActiveCategory='backup'; document.querySelector('#page-reports .subtab[data-subtab=\\'alerts\\']')?.click(); }, 80); return false;";
    const dot = (sev) => `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${sev==='alert'?'#e54':sev==='warn'?'#eb4':'#888'};flex-shrink:0"></span>`;
    const chip = (g) => `
      <a href="#" onclick="${jumpJs}"
         title="${escHtml(g.body)}"
         style="display:inline-flex;align-items:center;gap:6px;padding:3px 9px;background:var(--bg2,#1c1c1c);border:1px solid var(--border,#333);border-radius:999px;font-size:0.76rem;color:var(--text2,#ccc);text-decoration:none;max-width:340px">
        ${dot(g.severity)}
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtml(g.title)}</span>
        ${g.count > 1 ? `<span style="color:var(--text3);font-size:0.7rem;flex-shrink:0">×${g.count}</span>` : ''}
      </a>`;
    const TOP_N = 5;
    const top = grouped.slice(0, TOP_N);
    const moreCount = grouped.length - top.length;
    const moreLink = moreCount > 0
      ? `<a href="#" onclick="${jumpJs}" style="font-size:0.76rem;color:#7fc8ff;text-decoration:none;align-self:center">+${moreCount} more →</a>`
      : `<a href="#" onclick="${jumpJs}" style="font-size:0.76rem;color:#7fc8ff;text-decoration:none;align-self:center">Manage in Alerts →</a>`;
    strip.innerHTML = `
      <div class="card stripe-card is-warn" style="padding:10px 14px">
        <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:8px;gap:12px">
          <div style="font-size:0.82rem;font-weight:600;color:#eb4">⚠ Active backup alerts (${sigs.length})${sevSummary ? ` — ${escHtml(sevSummary)}` : ''}</div>
          <div style="font-size:0.72rem;color:var(--text3)">from the signal store</div>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
          ${top.map(chip).join('')}
          ${moreLink}
        </div>
      </div>`;
    strip.style.display = '';
  } catch (_e) {
    // Strip is a nice-to-have; never let it break the Backup page.
    strip.style.display = 'none';
  }
}

// ─────────────────────────────────────────────────────────────────────────
// renderBackupDiagnosticPanel(botId, b) — shared diagnostic renderer.
//
// Returns the HTML for an expandable sub-row that surfaces the per-bot
// failure context the /api/backup/cloud/status endpoint provides:
//   - classified_cause: {cause_id, title, fix_steps} via backup_diagnostics
//   - last_error: raw stderr (capped at 2KB by the writer)
//   - last_attempt_at / last_success_at: ISO timestamps
//   - drifted_keys: list of openclaw.json key drifts (per-bot)
//
// Reused by Backup → Status table AND Maintenance → Bot Versions
// table — both surfaces hit the same problem (bot's backup is broken
// and the diagnostic was only visible in a tooltip).
//
// Returns '' when there's nothing useful to show, so the caller can
// gate the expansion affordance on that.
function renderBackupDiagnosticPanel(botId, b) {
  if (!b) return '';
  const cause = b.classified_cause;
  const lastError = b.last_error;
  const drift = (b.drifted_keys || []).length;
  if (!cause && !lastError && drift === 0) return '';

  const fmtTs = (iso) => {
    if (!iso) return '<span style="color:var(--text3)">(none)</span>';
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return `<code style="font-size:0.78rem">${escHtml(iso)}</code>`;
    const h = (Date.now() - t) / 3600000;
    const rel = h < 1 ? `${Math.max(1, Math.round(h * 60))}m ago`
              : h < 48 ? `${Math.round(h)}h ago`
              : `${Math.round(h / 24)}d ago`;
    return `<code style="font-size:0.78rem">${escHtml(iso)}</code> <span style="color:var(--text3)">(${rel})</span>`;
  };

  const causeBlock = cause ? `
    <div class="stripe-card is-crit" style="margin-top:10px;padding:10px 12px;background:color-mix(in srgb, var(--red) 8%, transparent);border-radius:6px">
      <div style="font-size:0.82rem;font-weight:500;color:var(--text);margin-bottom:6px">
        Likely cause: ${escHtml(cause.title)}
      </div>
      <ol style="margin:0;padding-left:20px;font-size:0.8rem;color:var(--text);line-height:1.55">
        ${(cause.fix_steps || []).map(s => `<li style="margin-bottom:4px">${escHtml(s)}</li>`).join('')}
      </ol>
    </div>` : '';

  const errorBlock = lastError ? `
    <div style="margin-top:10px">
      <div style="font-size:0.74rem;color:var(--text3);margin-bottom:4px;font-weight:500;letter-spacing:0.04em">RAW ERROR</div>
      <pre style="margin:0;padding:10px 12px;background:var(--bg2);border:1px solid var(--border);border-radius:4px;font-size:0.74rem;color:var(--text2);overflow-x:auto;white-space:pre-wrap;word-break:break-word;max-height:240px">${escHtml(lastError)}</pre>
    </div>` : '';

  const driftBlock = drift > 0 ? `
    <div class="stripe-card is-warn" style="margin-top:10px;padding:8px 12px;background:color-mix(in srgb, var(--yellow) 8%, transparent);border-radius:6px;font-size:0.8rem">
      <strong style="color:var(--text)">Config drift:</strong>
      <code style="font-size:0.74rem;color:var(--text2)">${(b.drifted_keys || []).map(k => escHtml(k)).join(', ')}</code>
      — local openclaw.json values differ from the deployed baseline.
    </div>` : '';

  const lastAttempt = fmtTs(b.last_attempt_at);
  const lastSuccess = fmtTs(b.last_success_at);
  const consec = b.consecutive_failures || 0;
  const consecLine = consec >= 1
    ? `<div style="font-size:0.78rem;color:var(--text2)"><strong>${consec}</strong> consecutive failure${consec === 1 ? '' : 's'} since last success.</div>`
    : '';

  // Actions: retry + open Cloud tab for follow-on config changes.
  const actionsBlock = `
    <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn btn-ghost btn-sm" onclick="backupNowFromDiagnostic('${escHtml(botId)}', this)">
        ↻ Backup now
      </button>
      <button class="btn btn-ghost btn-sm" onclick="subTab(document.querySelector('#page-backup .subtab[data-subtab=&quot;cloud&quot;]'),'backup','cloud');loadBackupConfig();return false">
        Open Cloud config →
      </button>
    </div>`;

  return `
    <div class="stripe-card is-crit" style="padding:12px 14px;background:var(--bg2)">
      <div style="display:flex;gap:24px;flex-wrap:wrap;font-size:0.8rem;color:var(--text2)">
        <div><strong style="color:var(--text)">Last attempt:</strong> ${lastAttempt}</div>
        <div><strong style="color:var(--text)">Last success:</strong> ${lastSuccess}</div>
      </div>
      ${consecLine ? '<div style="margin-top:6px">' + consecLine + '</div>' : ''}
      ${causeBlock}
      ${errorBlock}
      ${driftBlock}
      ${actionsBlock}
    </div>`;
}

// Toggle a backup-diagnostic detail row by bot id. Used by both
// Backup → Status and Maintenance → Bot Versions tables. The detail
// row is rendered hidden alongside the parent row; this just flips
// display + rotates the chevron glyph.
function toggleBackupDiagnostic(botId, scope) {
  // scope distinguishes "bk" (Backup → Status) from "sysm"
  // (Maintenance) so the two surfaces can render expansions
  // independently without colliding ids.
  const detailRow = document.getElementById(`${scope}-bkdiag-${botId}`);
  const chevron = document.getElementById(`${scope}-bkdiag-chev-${botId}`);
  if (!detailRow) return;
  const isHidden = detailRow.style.display === 'none' || !detailRow.style.display;
  detailRow.style.display = isHidden ? '' : 'none';
  // chevron is now a .expand-icon SVG; toggle the .is-open class to
  // rotate it instead of swapping textContent. style-guide §9.13.
  if (chevron) chevron.classList.toggle('is-open', isHidden);
}

// "Backup now" action from inside the diagnostic panel. Reuses the
// same POST /api/backup/cloud/run endpoint the Cloud tab calls. Single
// inline button rather than a full status overlay; the user can hit
// Refresh on the table to see whether the run succeeded.
async function backupNowFromDiagnostic(botId, btn) {
  const orig = btn.textContent;
  btn.textContent = '↻ Running…';
  btn.disabled = true;
  try {
    const r = await api('POST', '/api/backup/cloud/run', { botId });
    if (r && r.ok) {
      btn.textContent = '✓ Started — refresh in a moment';
      setTimeout(() => { loadBackupStatusSummary(); }, 3000);
    } else {
      btn.textContent = '✗ ' + ((r && r.error) || 'failed');
      btn.disabled = false;
      setTimeout(() => { btn.textContent = orig; }, 4000);
    }
  } catch (e) {
    btn.textContent = '✗ ' + String(e).slice(0, 60);
    btn.disabled = false;
  }
}

async function loadBackupStatusSummary() {
  // Also refresh the Local mini-summary + backup signals strip in parallel.
  _loadLocalBackupMiniSummary();
  loadBackupStatusSignalsStrip();
  const el = document.getElementById('backup-status-summary');
  if (!el) return;
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  const data = await api('GET', '/api/backup/cloud/status');
  if (!data || data.error) {
    el.innerHTML = `<div class="alert alert-error">${escHtml(data?.error || 'Failed to load backup status')}</div>`;
    return;
  }
  const bots = data.bots || {};
  const ids = Object.keys(bots).sort();
  if (!data.any_configured || ids.length === 0) {
    el.innerHTML = `
      <div class="subtle" style="font-size:0.85rem">
        No bots are configured for cloud backup yet. Open the <a href="#" onclick="subTab(document.querySelector('#page-backup .subtab[data-subtab=&quot;cloud&quot;]'),'backup','cloud');loadBackupConfig();return false" style="color:var(--accent)">Cloud</a> tab to set up a backup repo for each bot.
      </div>`;
    return;
  }
  // Bucket configured bots only. Bots without a backupRepoUrl appear in
  // the response but they're not opted into backup, so they don't count
  // toward fresh/stale.
  const configuredIds = ids.filter(id => (bots[id] || {}).backup_configured);

  // Per-bot health derivation. The backend returns both:
  //   - last_backup  — timestamp of the most recent [backup]-tagged commit
  //                    found by `git log` (success record only)
  //   - last_attempt_status / last_attempt_at / last_error / consecutive_failures
  //                  — what the most recent daemon run actually did
  // Pre-2026-05-30 this function ignored attempt status entirely, so a bot
  // whose daemon had been skipping for 2 days with "github.pat not configured"
  // (or failing with EACCES) still rendered "✓ 17h ago" because the
  // [backup] commit history was unchanged. Operators saw 8/8 green while
  // backups were broken pod-wide. The derivation below treats a stale-but-
  // failing attempt as failing/blocked, NOT as the obsolete success.
  function deriveHealth(b) {
    if (b.needs_init) return { kind: 'needs_init' };
    const lastAttempt = b.last_attempt_at ? Date.parse(b.last_attempt_at) : NaN;
    const lastSuccess = b.last_success_at ? Date.parse(b.last_success_at) : NaN;
    const attempt = b.last_attempt_status;
    const attemptIsAuthoritative = Number.isFinite(lastAttempt)
      && (!Number.isFinite(lastSuccess) || lastAttempt > lastSuccess);
    if (attemptIsAuthoritative && attempt === 'failed') {
      return { kind: 'failing', error: b.last_error, attemptAt: b.last_attempt_at };
    }
    if (attemptIsAuthoritative && attempt === 'skipped') {
      return { kind: 'blocked', error: b.last_error, attemptAt: b.last_attempt_at };
    }
    // consecutive_failures > 0 covers the case where the *current* attempt
    // succeeded after a streak — still worth surfacing as "recent failures"
    // even though the freshness bucket is fresh.
    if (b.stale) return { kind: 'stale' };
    if (b.last_backup) return { kind: 'fresh' };
    return { kind: 'never' };
  }

  function relTime(iso) {
    if (!iso) return 'unknown';
    const ts = Date.parse(iso);
    if (!Number.isFinite(ts)) return 'unknown';
    const h = (Date.now() - ts) / 3600000;
    if (h < 1) return `${Math.max(1, Math.round(h * 60))}m ago`;
    if (h < 48) return `${Math.round(h)}h ago`;
    return `${Math.round(h / 24)}d ago`;
  }

  function firstLine(s) {
    return (s || '').split('\n')[0].slice(0, 240);
  }

  let fresh = 0, stale = 0, needsInit = 0, failing = 0, blocked = 0, drifted = 0;
  const healthById = {};
  configuredIds.forEach(id => {
    const b = bots[id] || {};
    const h = deriveHealth(b);
    healthById[id] = h;
    if (h.kind === 'needs_init') needsInit += 1;
    else if (h.kind === 'failing') failing += 1;
    else if (h.kind === 'blocked') blocked += 1;
    else if (h.kind === 'stale') stale += 1;
    else if (h.kind === 'fresh') fresh += 1;
    if (Array.isArray(b.drifted_keys) && b.drifted_keys.length > 0) drifted += 1;
  });
  // Header chips
  const chipStyle = 'display:inline-flex;align-items:center;gap:6px;padding:5px 12px;border-radius:14px;font-size:0.82rem;font-weight:500';
  const chips = [
    fresh > 0     ? `<span style="${chipStyle};background:rgba(46,213,115,0.12);color:#2ed573">✓ ${fresh} fresh</span>` : '',
    stale > 0     ? `<span style="${chipStyle};background:rgba(255,165,2,0.12);color:#ffa502">⚠ ${stale} stale</span>` : '',
    blocked > 0   ? `<span style="${chipStyle};background:rgba(255,165,2,0.12);color:#ffa502" title="Daemon ran but skipped — usually a config gap. Hover the row for the reason.">⊘ ${blocked} blocked (config)</span>` : '',
    needsInit > 0 ? `<span style="${chipStyle};background:rgba(127,140,141,0.12);color:var(--text2)">○ ${needsInit} needs init</span>` : '',
    failing > 0   ? `<span style="${chipStyle};background:rgba(255,71,87,0.12);color:#ff4757">✗ ${failing} failing</span>` : '',
    drifted > 0   ? `<span style="${chipStyle};background:rgba(255,71,87,0.12);color:#ff4757">↯ ${drifted} drifted</span>` : '',
  ].filter(Boolean).join(' ');
  // Per-bot rows. The "Last backup" cell now renders the most recent
  // *attempt* outcome when that attempt was failed/skipped (and newer than
  // the last success), not the obsolete green success time. The "Recent
  // failures" cell drops from `>= 3` to `>= 1` — a single fresh failure
  // should be visible, not hidden until the third bounce.
  const rows = configuredIds.map(id => {
    const b = bots[id] || {};
    const h = healthById[id];
    let status;
    if (h.kind === 'needs_init') {
      status = '<span style="color:var(--text2)">○ needs init</span>';
    } else if (h.kind === 'failing') {
      status = `<span style="color:#ff4757" title="${escHtml(firstLine(h.error) || 'failed')}">✗ failed ${escHtml(relTime(h.attemptAt))}</span>`;
    } else if (h.kind === 'blocked') {
      status = `<span style="color:#ffa502" title="${escHtml(firstLine(h.error) || 'skipped')}">⊘ blocked ${escHtml(relTime(h.attemptAt))}</span>`;
    } else if (h.kind === 'never' || !b.last_backup) {
      status = '<span style="color:var(--text2)">never</span>';
    } else {
      const hours = b.hours_ago == null ? null : Number(b.hours_ago);
      const ageLabel = hours == null
        ? 'unknown'
        : (hours < 48 ? `${Math.round(hours)}h ago` : `${Math.round(hours/24)}d ago`);
      const colour = h.kind === 'stale' ? '#ffa502' : '#2ed573';
      const glyph = h.kind === 'stale' ? '⚠' : '✓';
      status = `<span style="color:${colour}">${glyph} ${ageLabel}</span>`;
    }
    const failCount = b.consecutive_failures || 0;
    // Pin "recent failures" to the failing-or-blocked-current-attempt case
    // OR a non-zero counter. >= 3 was the old threshold and silenced single
    // failures — exactly the team_bot_c/security_bot "2× recent" case from 2026-05-30
    // which rendered gray when it should have been loud.
    const failCell = failCount >= 1
      ? `<span style="color:${failCount >= 3 ? '#ff4757' : '#ffa502'}" title="${escHtml(firstLine(b.last_error) || 'unknown')}">✗ ${failCount}× in a row</span>`
      : '<span style="color:var(--text2)">—</span>';
    const drift = (b.drifted_keys || []).length;
    const driftCell = drift > 0
      ? `<span style="color:#ff4757">↯ ${drift} key${drift === 1 ? '' : 's'}</span>`
      : '<span style="color:var(--text2)">—</span>';
    // Expansion affordance — only on rows where the operator can do
    // something useful with the expanded view: a failure / blocked /
    // stale state, a non-zero failure counter, or detected drift.
    // Fresh + needs_init rows stay quiet so the table doesn't grow
    // chrome on healthy bots.
    const hasDiagnostic = (
      h.kind === 'failing' || h.kind === 'blocked' || h.kind === 'stale'
      || failCount >= 1 || drift > 0
    );
    const panel = hasDiagnostic ? renderBackupDiagnosticPanel(id, b) : '';
    const expandable = hasDiagnostic && panel;
    const chevron = expandable
      ? `<span id="bk-bkdiag-chev-${escHtml(id)}" class="expand-icon" aria-hidden="true" style="color:var(--text3)"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span> `
      : '<span style="display:inline-block;width:14px"></span> ';
    const rowAttrs = expandable
      ? ` style="cursor:pointer" onclick="toggleBackupDiagnostic('${escHtml(id)}','bk')"`
      : '';
    const detailRow = expandable
      ? `<tr id="bk-bkdiag-${escHtml(id)}" style="display:none"><td colspan="4" style="padding:0">${panel}</td></tr>`
      : '';
    return `
      <tr${rowAttrs}>
        <td style="padding:6px 10px">${chevron}<strong>${escHtml(id)}</strong></td>
        <td style="padding:6px 10px">${status}</td>
        <td style="padding:6px 10px">${failCell}</td>
        <td style="padding:6px 10px">${driftCell}</td>
      </tr>${detailRow}`;
  }).join('');
  el.innerHTML = `
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px">${chips || '<span class="subtle" style="font-size:0.85rem">No backups recorded yet — open Cloud to run one.</span>'}</div>
    <table style="width:100%;border-collapse:collapse;font-size:0.85rem">
      <thead>
        <tr style="border-bottom:1px solid var(--border);color:var(--text2);font-size:0.78rem;text-align:left">
          <th style="padding:6px 10px;font-weight:500">Bot</th>
          <th style="padding:6px 10px;font-weight:500">Last backup</th>
          <th style="padding:6px 10px;font-weight:500">Recent failures</th>
          <th style="padding:6px 10px;font-weight:500">Config drift</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="subtle" style="font-size:0.78rem;margin-top:14px">
      For configuration changes, open the <a href="#" onclick="subTab(document.querySelector('#page-backup .subtab[data-subtab=&quot;cloud&quot;]'),'backup','cloud');loadBackupConfig();return false" style="color:var(--accent)">Cloud</a> tab. For restoring data, use <a href="#" onclick="subTab(document.querySelector('#page-backup .subtab[data-subtab=&quot;recovery&quot;]'),'backup','recovery');recoveryRefresh();podCommitsRefresh();return false" style="color:var(--accent)">Recovery</a>. To rotate the pod-wide GitHub PAT, <a href="#" onclick="openGithubBackupRotate();return false" style="color:var(--accent)">re-run the GitHub setup wizard →</a>
    </div>`;
}

// Phase 2: Status-tab mini-summary of TM state. Single line. The full
// rendering lives on the Local tab.
async function _loadLocalBackupMiniSummary() {
  const el = document.getElementById('backup-status-local-summary');
  if (!el) return;
  const data = await api('GET', '/api/backup/local/status');
  if (!data || data.available === false) {
    el.innerHTML = `<span class="subtle">Time Machine isn't available on this host.</span>`;
    return;
  }
  if (data.error) {
    el.innerHTML = `<span class="subtle">Couldn't read Time Machine state: ${escHtml(data.error)}</span>`;
    return;
  }
  if (!data.configured) {
    el.innerHTML = `
      <span style="color:#ffa502">○ Not configured.</span>
      Plug in a drive or set up a NAS, then open the <a href="#" onclick="subTab(document.querySelector('#page-backup .subtab[data-subtab=&quot;local&quot;]'),'backup','local');loadLocalBackupStatus();return false" style="color:var(--accent)">Local</a> tab for setup guidance.`;
    return;
  }
  const h = data.last_backup_hours_ago;
  const inProgress = !!data.in_progress;
  let line;
  if (inProgress) {
    line = `<span style="color:#3742fa">↻ Backing up now.</span>`;
  } else if (h == null) {
    line = `<span style="color:#ffa502">⚠ Configured, but no backup has completed yet.</span>`;
  } else {
    const fresh = h < 48;
    const ageLabel = h < 48 ? `${Math.round(h)}h ago` : `${Math.round(h/24)}d ago`;
    const colour = fresh ? '#2ed573' : '#ffa502';
    const glyph = fresh ? '✓' : '⚠';
    line = `<span style="color:${colour}">${glyph} Last backup ${ageLabel}.</span>`;
  }
  const dCount = (data.destinations || []).length;
  const dLabel = dCount === 1 ? '1 destination' : `${dCount} destinations`;
  const excludedNote = (data.excluded_pod_paths || []).length > 0
    ? ` <span style="color:#ff4757">⚠ ${data.excluded_pod_paths.length} pod path(s) excluded.</span>`
    : '';
  el.innerHTML = `
    ${line} ${dLabel}.${excludedNote}
    <a href="#" onclick="subTab(document.querySelector('#page-backup .subtab[data-subtab=&quot;local&quot;]'),'backup','local');loadLocalBackupStatus();return false" style="color:var(--accent);margin-left:8px">Details →</a>`;
}

// ── Phase 3b: Data classification UI ─────────────────────────────────────
//
// Loads /api/backup/data/overview, renders per-bot per-app cards with a
// three-tier picker, plus a pod-wide section that edits
// network.json::backup. Tier copy is intentionally simple — operators
// pick from three named outcomes rather than authoring privacy fields by
// hand. Per-folder per-app editing was dropped (see notes on _BACKUP_TIERS).

// Three operator-pickable tiers. ``some_data_local`` exists on the
// backend for historical / mixed-state manifests but is intentionally
// not offered as a UI choice: per-folder fidelity proved more confusing
// than useful, and "some_data_local" was a no-op tier that wouldn't
// stick when picked. Mixed-state manifests (e.g. authored by the
// scanner before this simplification) show no radio selected with a
// "Custom rules in place" note — picking any of the three normalises
// them into one of the standard modes.
const _BACKUP_TIERS = [
  {
    id: "whole_app_local",
    label: "Whole app local",
    desc: "Nothing about this app leaves the mini — not data, not the app's own code.",
  },
  {
    id: "all_data_local",
    label: "All data local",
    desc: "App code can be cloud-backed up so you can restore it on a new mini. Data stays local.",
  },
  {
    id: "full_cloud",
    label: "Full cloud",
    desc: "Everything in this app is eligible for cloud backup.",
  },
];

// Pod-wide per-folder editor still uses these (Add/edit row dropdown).
// Per-app per-folder editor has been removed in favour of the 3-tier picker,
// so this only feeds the pod-wide section.
const _BACKUP_PRIVACY_OPTIONS = [
  { id: "cloud",     label: "Cloud" },
  { id: "local",     label: "Local-only" },
  { id: "ephemeral", label: "Ephemeral" },
];

let _backupDataState = null;

async function loadBackupDataOverview() {
  const el = document.getElementById('backup-data-apps');
  const podBody = document.getElementById('backup-data-pod-body');
  if (!el || !podBody) return;
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  podBody.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  const data = await api('GET', '/api/backup/data/overview');
  if (!data || data.error) {
    el.innerHTML = `<div class="alert alert-error">${escHtml(data?.error || 'Failed to load data classification')}</div>`;
    podBody.innerHTML = '';
    return;
  }
  _backupDataState = data;
  _renderBackupDataApps();
  _renderBackupDataPod();
}

function _renderBackupDataApps() {
  const el = document.getElementById('backup-data-apps');
  if (!el || !_backupDataState) return;
  const bots = _backupDataState.bots || {};
  const botIds = orderedBotIds(bots);
  if (botIds.length === 0) {
    el.innerHTML = `<div class="card subtle" style="font-size:0.85rem">No bots configured.</div>`;
    return;
  }
  const legend = _renderBackupDataLegend();
  const botCards = botIds.map(botId => {
    const botEntry = bots[botId] || {};
    const apps = (botEntry.apps || []).slice().sort((a, b) => a.name.localeCompare(b.name));
    if (apps.length === 0) {
      return `
        <div class="card">
          <h3 style="margin:0 0 8px">${escHtml(botLabel(botId))}</h3>
          ${_renderBotDefaultPicker(botId, botEntry.backup_default_tier || "")}
          <div class="subtle" style="font-size:0.82rem;margin-top:14px">No apps installed on this bot yet.</div>
        </div>`;
    }
    return `
      <div class="card">
        <h3 style="margin:0 0 14px">${escHtml(botLabel(botId))}</h3>
        ${_renderBotDefaultPicker(botId, botEntry.backup_default_tier || "")}
        ${_renderBotAppsTable(botId, apps)}
      </div>`;
  }).join('');
  el.innerHTML = legend + botCards;
}

// Tier legend shown once at the top of the Data subtab so each
// per-app row can just be a radio without re-stating the definitions.
function _renderBackupDataLegend() {
  return `
    <div class="card" style="padding:14px 16px">
      <div style="font-size:0.78rem;color:var(--text2);font-weight:600;text-transform:uppercase;letter-spacing:.04em;margin-bottom:8px">Tier key</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px">
        ${_BACKUP_TIERS.map(t => `
          <div>
            <div style="font-weight:600;font-size:0.85rem;margin-bottom:2px">${escHtml(t.label)}</div>
            <div class="subtle" style="font-size:0.78rem;line-height:1.4">${escHtml(t.desc)}</div>
          </div>
        `).join('')}
      </div>
    </div>`;
}

// Per-bot default-tier picker. Sits at the top of each bot card.
// Clicking a button applies the tier to every current app on the bot
// AND records it as the default for future installs — no confirmation
// prompt. Per-app overrides happen below the picker, after the fact.
function _renderBotDefaultPicker(botId, currentDefault) {
  const buttons = _BACKUP_TIERS.map(t => {
    const isSelected = t.id === currentDefault;
    return `
      <button
        class="btn btn-sm ${isSelected ? 'btn-primary' : 'btn-ghost'}"
        onclick="backupDataSetBotDefault('${escHtml(botId)}', '${t.id}')"
        title="${escHtml(t.desc)}">
        ${escHtml(t.label)}
      </button>`;
  }).join('');
  const clearLink = currentDefault
    ? `<a href="#" onclick="backupDataSetBotDefault('${escHtml(botId)}', '');return false" style="font-size:0.78rem;color:var(--text2);margin-left:8px">Clear default</a>`
    : '';
  return `
    <div style="background:var(--bg2);padding:12px;border-radius:6px;margin-bottom:18px">
      <div style="font-size:0.78rem;color:var(--text2);font-weight:600;margin-bottom:4px">Default for this bot's apps</div>
      <div class="subtle" style="font-size:0.74rem;margin-bottom:10px">Picking a button applies it to every app on this bot and stamps it on future installs. Override per app below.</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">${buttons}${clearLink}</div>
    </div>`;
}

// Per-bot apps table. One row per app, one radio per tier — the tier
// definitions live in the legend at the top of the section. Unclassified
// apps render with no radio selected; clicking any column writes that
// tier via the same PATCH the per-app radio uses. Mixed-state manifests
// (back-end ``some_data_local``) also render unselected, with a small
// note that picking a tier normalises them.
function _renderBotAppsTable(botId, apps) {
  const colHeaderStyle = 'text-align:center;padding:8px 6px;font-size:0.72rem;color:var(--text2);text-transform:uppercase;letter-spacing:.04em;font-weight:600;width:14%';
  const rows = apps.map(app => {
    const currentTier = app.current_tier;
    const isMixed = currentTier === "some_data_local";
    const isUnclassified = currentTier === "unclassified";
    const cells = _BACKUP_TIERS.map(t => {
      const checked = t.id === currentTier;
      return `
        <td style="text-align:center;padding:8px 6px;background:${checked ? 'rgba(124,92,255,0.08)' : 'transparent'}">
          <label style="display:block;cursor:pointer;padding:4px 0" title="${escHtml(t.desc)}">
            <input type="radio" name="tier-${escHtml(botId)}-${escHtml(app.id)}" value="${t.id}"
                   ${checked ? 'checked' : ''}
                   onchange="backupDataSetTier('${escHtml(botId)}','${escHtml(app.id)}','${t.id}')">
          </label>
        </td>`;
    }).join('');
    const note = isMixed
      ? `<div class="subtle" style="font-size:0.72rem;color:var(--accent);margin-top:2px">Custom per-folder rules — pick a tier to replace</div>`
      : (isUnclassified
        ? `<div class="subtle" style="font-size:0.72rem;margin-top:2px">Not classified yet</div>`
        : '');
    return `
      <tr style="border-bottom:1px solid var(--border)">
        <td style="padding:8px 10px;font-size:0.85rem">
          <div style="font-weight:500">${escHtml(app.name)}</div>
          ${note}
        </td>
        ${cells}
      </tr>`;
  }).join('');
  return `
    <table style="width:100%;border-collapse:collapse">
      <thead>
        <tr style="border-bottom:1px solid var(--border)">
          <th style="text-align:left;padding:8px 10px;font-size:0.72rem;color:var(--text2);text-transform:uppercase;letter-spacing:.04em;font-weight:600">App</th>
          ${_BACKUP_TIERS.map(t => `<th style="${colHeaderStyle}" title="${escHtml(t.desc)}">${escHtml(t.label)}</th>`).join('')}
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function _renderBackupDataPod() {
  const el = document.getElementById('backup-data-pod-body');
  if (!el || !_backupDataState) return;
  const pod = _backupDataState.pod_wide || {};
  const defaultVal = pod.default_for_unclassified || "";
  const paths = pod.data_paths || [];

  // Empty-state question — fires when no pod-wide classification has
  // been declared at all. Mirrors the per-app empty-state pattern from
  // the spec: ask the operator the question that matters in plain
  // language, give them three jump-off buttons. Picking "Cloud default"
  // or "Local-only" writes the corresponding pod-wide default and the
  // controls below render in their normal mode; "I'll set it per-folder"
  // is a no-op that surfaces the rules editor without writing anything.
  const isEmpty = !defaultVal && paths.length === 0 && !pod._editor_active;
  if (isEmpty) {
    el.innerHTML = `
      <div style="padding:14px 0">
        <div style="font-weight:600;margin-bottom:8px">Pod-wide data hasn't been classified yet.</div>
        <div class="subtle" style="font-size:0.85rem;margin-bottom:14px">
          For data outside any one app's territory — proposals, signals, observations, and other shared state — would new files default to cloud backup or stay local-only on this mini?
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-primary btn-sm" onclick="backupDataSetPodDefault('cloud')">Cloud (default)</button>
          <button class="btn btn-ghost btn-sm" onclick="backupDataSetPodDefault('local')">Local-only</button>
          <button class="btn btn-ghost btn-sm" onclick="_backupDataPodSkipQuestion()">I'll set it per-folder</button>
        </div>
      </div>`;
    return;
  }

  const defaultOptions = [
    { id: "",          label: "(not set — falls back to cloud)" },
    { id: "cloud",     label: "Cloud" },
    { id: "local",     label: "Local-only" },
    { id: "ephemeral", label: "Ephemeral (don't back up)" },
  ];
  const rows = paths.map((p, idx) => `
    <tr>
      <td style="padding:4px 8px"><code>${escHtml(p.path)}</code></td>
      <td style="padding:4px 8px">
        <select class="input-w-md" onchange="backupDataUpdatePodPathPrivacy(${idx},this.value)" style="font-size:0.82rem">
          ${_BACKUP_PRIVACY_OPTIONS.map(o => `<option value="${o.id}" ${o.id === p.privacy ? 'selected' : ''}>${escHtml(o.label)}</option>`).join('')}
        </select>
      </td>
      <td style="padding:4px 8px;text-align:right">
        <button class="btn btn-ghost btn-sm" onclick="backupDataRemovePodPath(${idx})" title="Remove">✕</button>
      </td>
    </tr>
  `).join('');
  el.innerHTML = `
    <div style="margin-bottom:18px">
      <div style="font-size:0.78rem;color:var(--text2);font-weight:600;margin-bottom:6px">Default for unclassified files</div>
      <div style="display:flex;gap:8px;align-items:center">
        <select id="backup-pod-default" class="input-w-md" onchange="backupDataSetPodDefault(this.value)" style="font-size:0.85rem">
          ${defaultOptions.map(o => `<option value="${o.id}" ${o.id === defaultVal ? 'selected' : ''}>${escHtml(o.label)}</option>`).join('')}
        </select>
        <span class="subtle" style="font-size:0.78rem">Applies to anything in pod-wide storage that no rule below or per-app rule covers.</span>
      </div>
    </div>
    <div style="font-size:0.78rem;color:var(--text2);font-weight:600;margin-bottom:6px">Pod-wide path rules</div>
    ${paths.length > 0 ? `
      <table style="width:100%;font-size:0.82rem;border-collapse:collapse;margin-bottom:10px">
        <thead><tr style="color:var(--text2);font-size:0.74rem;border-bottom:1px solid var(--border)">
          <th style="text-align:left;padding:4px 8px;font-weight:500">Path</th>
          <th style="text-align:left;padding:4px 8px;font-weight:500">Classification</th>
          <th></th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    ` : '<div class="subtle" style="font-size:0.78rem;margin-bottom:10px">No pod-wide rules declared yet — add one below to override the default for a specific directory.</div>'}
    <div style="display:flex;gap:6px">
      <input id="backup-pod-path-add" placeholder="path/relative/to/shared-storage/" style="flex:1;font-size:0.82rem;padding:5px 8px;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text)">
      <select id="backup-pod-priv-add" class="input-w-md" style="font-size:0.82rem">
        ${_BACKUP_PRIVACY_OPTIONS.map(o => `<option value="${o.id}">${escHtml(o.label)}</option>`).join('')}
      </select>
      <button class="btn btn-primary btn-sm" onclick="backupDataAddPodPath()">Add</button>
    </div>`;
}

// Empty-state "I'll set it per-folder" path: don't write anything; just
// flip the local view so the rules editor appears (using a sentinel that
// we treat as "operator opened the editor"). Simplest implementation:
// shove an empty-but-non-default state into the local cache and re-render.
function _backupDataPodSkipQuestion() {
  if (!_backupDataState || !_backupDataState.pod_wide) return;
  // Sentinel: pod-wide gets a transient "intent to author" marker so the
  // editor view renders without a pending PATCH. Next operator action
  // (add a path or pick a default) persists for real.
  _backupDataState.pod_wide._editor_active = true;
  _renderBackupDataPod();
}

// ── Mutators (all PATCH back to the server, then re-render from response) ──

async function _setBackupDataStatus(text, ok = true) {
  const el = document.getElementById('backup-data-status-line');
  if (!el) return;
  el.style.color = ok ? 'var(--text2)' : '#ff4757';
  el.textContent = text;
  if (ok && text) setTimeout(() => { if (el.textContent === text) el.textContent = ''; }, 3000);
}

async function backupDataSetTier(botId, appId, tier) {
  _setBackupDataStatus('Saving…');
  const resp = await api('PATCH', '/api/backup/data/app', {
    bot_id: botId, app_id: appId, tier,
  });
  if (!resp || !resp.ok) {
    _setBackupDataStatus(`Save failed: ${resp?.error || 'unknown error'}`, false);
    return;
  }
  _setBackupDataStatus(`Saved — ${appId} is now ${resp.current_tier.replace(/_/g, ' ')}`);
  loadBackupDataOverview();
}

// Per-bot default tier handler. Picking a real tier applies it
// immediately to every installed app for that bot AND stamps it on
// future installs — no prompts. The empty string just clears the
// default.
async function backupDataSetBotDefault(botId, tier) {
  _setBackupDataStatus('Saving…');
  if (!tier) {
    const resp = await api('PATCH', '/api/backup/data/bot', {
      bot_id: botId, tier: "",
    });
    if (!resp || !resp.ok) {
      _setBackupDataStatus(`Save failed: ${resp?.error || 'unknown error'}`, false);
      return;
    }
    _setBackupDataStatus(`Cleared default for ${botId}`);
    loadBackupDataOverview();
    return;
  }
  const tierLabel = tier.replace(/_/g, ' ');
  const resp = await api('PATCH', '/api/backup/data/bot', {
    bot_id: botId, tier, apply_to_existing: true,
  });
  if (!resp || !resp.ok) {
    _setBackupDataStatus(`Save failed: ${resp?.error || 'unknown error'}`, false);
    return;
  }
  const updated = resp.apps_updated || 0;
  const tail = updated > 0
    ? `applied to ${updated} app${updated === 1 ? '' : 's'}`
    : 'set for future apps';
  _setBackupDataStatus(`Saved — ${botId} default is now ${tierLabel}, ${tail}`);
  loadBackupDataOverview();
}

async function backupDataSetPodDefault(value) {
  _setBackupDataStatus('Saving…');
  const resp = await api('PATCH', '/api/backup/data/pod', {
    default_for_unclassified: value,
  });
  if (!resp || !resp.ok) {
    _setBackupDataStatus(`Save failed: ${resp?.error || 'unknown error'}`, false);
    return;
  }
  _setBackupDataStatus('Saved.');
  loadBackupDataOverview();
}

async function backupDataAddPodPath() {
  const inp = document.getElementById('backup-pod-path-add');
  const sel = document.getElementById('backup-pod-priv-add');
  if (!inp || !sel) return;
  const path = (inp.value || '').trim();
  const privacy = sel.value;
  if (!path) { _setBackupDataStatus('Enter a path first.', false); return; }
  const newPaths = [...((_backupDataState?.pod_wide?.data_paths) || []), { path, privacy }];
  await _patchPodDataPaths(newPaths);
}

async function backupDataRemovePodPath(idx) {
  const cur = (_backupDataState?.pod_wide?.data_paths) || [];
  const newPaths = cur.filter((_, i) => i !== idx);
  await _patchPodDataPaths(newPaths);
}

async function backupDataUpdatePodPathPrivacy(idx, newPrivacy) {
  const cur = (_backupDataState?.pod_wide?.data_paths) || [];
  const newPaths = cur.map((p, i) =>
    i === idx ? { ...p, privacy: newPrivacy } : p
  );
  await _patchPodDataPaths(newPaths);
}

async function _patchPodDataPaths(dataPaths) {
  _setBackupDataStatus('Saving…');
  const resp = await api('PATCH', '/api/backup/data/pod', {
    data_paths: dataPaths,
  });
  if (!resp || !resp.ok) {
    _setBackupDataStatus(`Save failed: ${resp?.error || 'unknown error'}`, false);
    return;
  }
  _setBackupDataStatus('Saved.');
  loadBackupDataOverview();
}

// Decide what to render in place of the old "Open Time Machine settings"
// button. The x-apple.systempreferences:… URL only acts on the machine
// running the browser, so it's only useful when the operator is at the
// same Mac as the admin daemon. Otherwise we name the host instead and
// tell them to log in there.
function _renderLocalBackupOpenSettings(daemonHostname) {
  const slot = document.getElementById('backup-local-open-settings');
  if (!slot) return;
  const browserHost = (window.location.hostname || '').toLowerCase();
  const dh = (daemonHostname || '').toLowerCase();
  // Same-host heuristic: hostnames match (with or without ".local"). We
  // deliberately don't treat "localhost"/"127.0.0.1" as same-host — an
  // SSH tunnel makes the browser look local while the daemon is remote,
  // and the URL scheme would silently open the wrong machine's settings.
  const stripLocal = (h) => h.replace(/\.local$/, '');
  const sameHost = dh && browserHost && stripLocal(browserHost) === stripLocal(dh);
  if (sameHost) {
    slot.innerHTML = `<button class="btn btn-primary btn-sm" onclick="window.location.href='x-apple.systempreferences:com.apple.preference.timemachine'">Open Time Machine settings</button>`;
  } else {
    const hostLabel = dh ? `<code>${escHtml(dh)}</code>` : 'the pod host';
    slot.innerHTML = `<span class="subtle" style="font-size:0.78rem">Time Machine is configured on ${hostLabel} — open System Settings → Time Machine there (Screen Sharing, or sign in at the machine).</span>`;
  }
}

// Phase 2: Local backup (Time Machine) status panel. Endpoint is cheap and
// can be polled; the Refresh button + tab activate both fire this.
async function loadLocalBackupStatus() {
  const el = document.getElementById('backup-local-panel');
  const statusLine = document.getElementById('backup-local-status-line');
  if (!el) return;
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  if (statusLine) statusLine.textContent = '';
  const data = await api('GET', '/api/backup/local/status');
  if (!data) {
    el.innerHTML = `<div class="alert alert-error">Failed to load local backup status.</div>`;
    return;
  }
  _renderLocalBackupOpenSettings(data.hostname);
  if (data.available === false) {
    el.innerHTML = `
      <div class="subtle" style="font-size:0.85rem">
        Time Machine isn't available on this system (only macOS hosts have <code>tmutil</code>). The Local tab has nothing to show here.
      </div>`;
    return;
  }
  if (data.error) {
    el.innerHTML = `<div class="alert alert-error">${escHtml(data.error)}</div>`;
    return;
  }
  if (!data.configured) {
    el.innerHTML = `
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px">
        <span style="font-size:1.4rem">○</span>
        <div>
          <div style="font-weight:600">Time Machine is not configured</div>
          <div class="subtle" style="font-size:0.82rem">No local backup destination set. Plug in a drive (or set up a NAS) and add it in System Settings → Time Machine.</div>
        </div>
      </div>
      <div class="subtle" style="font-size:0.78rem">
        Once you configure a destination, this panel picks up the change within an hour.
      </div>`;
    return;
  }
  // Configured: render hero status + destinations + exclusions
  const hours = data.last_backup_hours_ago;
  const inProgress = !!data.in_progress;
  let heroGlyph, heroColour, heroText, heroSub;
  if (inProgress) {
    heroGlyph = '↻'; heroColour = '#3742fa';
    heroText = 'Backing up now';
    heroSub = 'A Time Machine backup is currently running.';
  } else if (hours == null) {
    heroGlyph = '⚠'; heroColour = '#ffa502';
    heroText = 'Configured but never run';
    heroSub = 'A destination is set, but no backup has completed yet. Open System Settings → Time Machine and click "Back Up Now" if the first one didn\'t auto-trigger.';
  } else {
    const fresh = hours < 48;
    const ageLabel = hours < 48 ? `${Math.round(hours)}h ago` : `${Math.round(hours/24)}d ago`;
    heroGlyph = fresh ? '✓' : '⚠';
    heroColour = fresh ? '#2ed573' : '#ffa502';
    heroText = `Last backup ${ageLabel}`;
    heroSub = data.last_backup_at ? `Snapshot taken ${escHtml(data.last_backup_at)}.` : '';
  }
  // Destinations list
  const dests = (data.destinations || []).map(d => {
    const isActive = d.last_destination ? '<span style="color:#2ed573;font-weight:500">● active</span>' : '';
    const mount = d.mount_point ? `<code>${escHtml(d.mount_point)}</code>` : '<span class="subtle">not mounted</span>';
    const free = d.bytes_available ? `${(d.bytes_available / 1e9).toFixed(1)} GB free` : '';
    return `
      <li style="margin-bottom:6px">
        <strong>${escHtml(d.name)}</strong>
        <span class="subtle" style="font-size:0.78rem">(${escHtml(d.kind)})</span>
        ${isActive}<br>
        <span class="subtle" style="font-size:0.78rem">${mount}${free ? ' · ' + escHtml(free) : ''}</span>
      </li>`;
  }).join('');
  // Exclusions warning
  const excluded = data.excluded_pod_paths || [];
  let exclusionsBlock = '';
  if (excluded.length > 0) {
    const items = excluded.map(p => `<li><code>${escHtml(p)}</code></li>`).join('');
    exclusionsBlock = `
      <div class="alert stripe-card is-crit" style="background:color-mix(in srgb, var(--red) 8%, transparent);padding:10px;margin-top:12px;border-radius:6px">
        <div style="font-weight:600;margin-bottom:4px">⚠ Pod paths excluded from Time Machine</div>
        <div class="subtle" style="font-size:0.82rem;margin-bottom:8px">These directories won't be in TM snapshots, even though TM looks healthy:</div>
        <ul class="subtle" style="font-size:0.82rem;margin:0;padding-left:18px">${items}</ul>
        <div class="subtle" style="font-size:0.78rem;margin-top:8px">Open System Settings → Time Machine → Options to remove them from the exclusion list.</div>
      </div>`;
  } else {
    exclusionsBlock = `
      <div class="subtle" style="font-size:0.78rem;margin-top:12px">
        ✓ Pod paths are included in Time Machine backups (shared directory + Evolve deploy checkout).
      </div>`;
  }
  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:18px">
      <span style="font-size:1.6rem;color:${heroColour}">${heroGlyph}</span>
      <div>
        <div style="font-weight:600;font-size:1.05rem;color:${heroColour}">${escHtml(heroText)}</div>
        <div class="subtle" style="font-size:0.82rem">${heroSub}</div>
      </div>
    </div>
    <div style="font-size:0.82rem;color:var(--text2);font-weight:600;margin-bottom:6px">Destinations</div>
    <ul style="font-size:0.85rem;list-style:none;padding:0;margin:0">${dests || '<li class="subtle">No destinations.</li>'}</ul>
    ${exclusionsBlock}`;
}

async function loadBackupConfig() {
  const el = document.getElementById('backup-config-panel');
  if (!el) return;
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  const d = await api('GET', '/api/backup/cloud/config');
  if (!d || d.error) {
    el.innerHTML = `<div class="alert alert-error">${escHtml(d?.error || 'Failed to load backup config')}</div>`;
    return;
  }
  // Also fetch backup status for last-backup timestamps
  const status = await api('GET', '/api/backup/cloud/status');
  _backupCfgData = d;
  renderBackupConfig(d, status);

  // Surface backup-drift state to evo's page-context. Without this,
  // a 2026-05-20 transcript showed evo on this subtab confabulating
  // about audit-finding Mute (a different mechanism) when the operator
  // asked "accept all configs as baseline" — because the drift list
  // visible to the operator was invisible to evo. We write into the
  // 'security' snapshot bucket so _EVO_CONTEXT_PACKS.security folds
  // it in without a separate fetch.
  try {
    const statusBots = (status && status.bots) || {};
    const drifted = [];
    for (const [botId, info] of Object.entries(statusBots)) {
      const keys = Array.isArray(info?.drifted_keys) ? info.drifted_keys : [];
      if (keys.length) {
        drifted.push({
          bot_id: botId,
          drifted_keys: keys,
          stale_backup: !!info?.stale,
          last_backup_at: info?.last_backup || null,
        });
      }
    }
    window._evoContextSnapshots = window._evoContextSnapshots || {};
    const prev = window._evoContextSnapshots.security || {};
    window._evoContextSnapshots.security = {
      ...prev,
      active_subtab: 'backups',
      backup_drift: drifted,
    };
    if (typeof _evoDrawerUpdateContextChip === 'function') _evoDrawerUpdateContextChip();
  } catch (_) {}
}

// /api/backup/cloud/config appends a legacy `evolve` id (routes_oc.py) for
// pre-#3053 pods whose backup config lived on the top-level `evolve`
// account. A pod that has moved to a real primary id carries no
// `bots.evolve` in network.json — render no card for it rather than a
// phantom (EVO-SEP-S4).
//
// orderedBotIds cannot make this call itself: these records are
// config-ADJACENT, not config ({backupRepoUrl, pubkey}), and an id the
// roster doesn't carry is kept there by design so synthesized maps aren't
// culled. With no roster loaded we keep everything — an unavailable
// network.json is not evidence that a bot doesn't exist.
function _backupVisibleBots(cfgBots) {
  const roster = (_networkData && _networkData.bots) || {};
  const rosterKnown = Object.keys(roster).length > 0;
  if (!rosterKnown || ('evolve' in roster)) return { ...(cfgBots || {}) };
  return Object.fromEntries(
    Object.entries(cfgBots || {}).filter(([id]) => id !== 'evolve')
  );
}

function renderBackupConfig(cfg, status) {
  const el = document.getElementById('backup-config-panel');
  if (!el) return;
  const bots = _backupVisibleBots(cfg.bots);
  const statusBots = status?.bots || {};
  // network.json::defaultBackupAccount feeds the URL placeholder so
  // operators see the right form at a glance. Per-bot URLs can still
  // point elsewhere (e.g. team_bot_a lives under evolve-ops).
  const defaultAccount = (cfg.default_account || 'org');

  // Distribute-one-key card — operator clicks once to generate a single
  // SSH private key, copy it into every bot's ~/.ssh/evolve-backup-<bot>
  // (bot-owned), and surface the pubkey for one-shot GitHub registration.
  // Per-bot per-repo keys still possible (use Generate SSH key per bot
  // and put different values into each bot's file).
  //
  // Each per-bot card below shows ITS OWN pubkey — when distributed,
  // every card shows the same value (the visual cue that the same key
  // is in use). When per-bot generation is used, each card shows its
  // own distinct value.
  const sharedKeyCard = `<div class="card" style="margin-bottom:14px;border-left:3px solid var(--accent)">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap">
        <span style="font-weight:600">Distribute one backup key</span>
      </div>
      <div style="font-size:0.78rem;color:var(--text2);margin-top:6px">
        Generate one private key, copy it into every configured bot's
        <code>~/.ssh/</code>, and register the pubkey ONCE on your GitHub
        user account. Works for every repo that user owns or can push to,
        across personal and org accounts. Per-repo deploy keys remain an
        option (use the per-bot Generate buttons below for per-bot values).
      </div>
      <div style="display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap">
        <button class="btn btn-primary btn-sm" id="backup-distribute-btn" onclick="backupDistributeKey()">Generate + distribute</button>
        <button class="btn btn-ghost btn-sm" id="backup-pushtest-btn" onclick="backupRunPushTest()" title="Run git ls-remote against each bot's backup repo. Surfaces auth/access failures that registration success doesn't catch (e.g. user has read access via collab, but no SSH push to the org repo).">Run push test</button>
        <span id="backup-distribute-status" style="font-size:0.78rem;color:var(--text2)"></span>
      </div>
      <div id="backup-distribute-result" style="margin-top:10px"></div>
      <div id="backup-pushtest-result" style="margin-top:10px"></div>
    </div>`;

  const cards = orderedBotIds(bots).map(botId => [botId, bots[botId]]).map(([botId, info]) => {
    const url = info.backupRepoUrl || '';
    const pubkey = info.pubkey || '';
    const bs = statusBots[botId] || {};
    const configured = !!url;
    // Status badge logic — check `configured` FIRST so a stale [backup]
    // commit left over from a previous setup can't flash a misleading
    // green "✓ 65.2h ago" when the bot isn't backing up anymore. Then
    // check the run-state file: if the most recent attempt FAILED, show
    // a red badge with the captured error in the tooltip — a successful
    // last_backup timestamp from days ago can't masquerade as "fresh"
    // when every nightly attempt since has been bouncing.
    const lastAttempt = bs.last_attempt_at;
    const lastSuccess = bs.last_success_at;
    const recentFailure = configured
      && lastAttempt
      && (bs.last_attempt_status === 'failed' || (lastSuccess && lastAttempt > lastSuccess));
    let lastBackup;
    if (!configured) {
      lastBackup = '<span style="color:var(--text3)">not configured</span>';
    } else if (bs.needs_init) {
      lastBackup = `<span style="color:var(--warning)">⚠ workspace not initialized</span> <button class="btn btn-ghost btn-sm" onclick="backupInitWorkspace('${escHtml(botId)}')" id="backup-init-btn-${escHtml(botId)}">Init now</button>`;
    } else if (recentFailure) {
      const tFail = Date.parse(lastAttempt);
      const fage = Number.isFinite(tFail)
        ? `${Math.round((Date.now() - tFail) / 3600000)}h ago`
        : 'recently';
      const errTip = (bs.last_error || 'see backup logs').slice(0, 400);
      const consec = bs.consecutive_failures || 1;
      lastBackup = `<span style="color:var(--red)" title="${escHtml(errTip)}">✗ push failed ${escHtml(fage)}${consec > 1 ? ` · ${consec}× in a row` : ''}</span>`;
    } else if (bs.git_error) {
      lastBackup = `<span style="color:var(--red)" title="${escHtml(bs.git_error)}">⛔ git error — hover for details</span>`;
    } else if (bs.last_backup) {
      const age = bs.hours_ago != null ? `${bs.hours_ago}h ago` : 'never';
      lastBackup = bs.stale
        ? `<span style="color:var(--warning)">⚠ ${escHtml(age)}</span>`
        : `<span style="color:var(--green)">✓ ${escHtml(age)}</span>`;
    } else {
      lastBackup = '<span style="color:var(--text3)">no backups yet</span>';
    }
    // "Backup now" button — only enabled when both the URL is set AND the
    // workspace is a git repo. Otherwise the operator has setup to do first.
    const canBackupNow = configured && !bs.needs_init;
    const backupNowBtn = `<button class="btn btn-ghost btn-sm" id="backup-now-btn-${escHtml(botId)}" onclick="backupNow('${escHtml(botId)}')"${canBackupNow ? '' : ' disabled'}>Backup now</button>`;

    // Per-bot SSH key block — every bot has its own ~/.ssh/evolve-backup-<bot>
    // file. The VALUE may be the same across bots (after "Distribute one key")
    // or different (after per-bot Generate, for per-repo deploy keys).
    // Operator choice; the panel renders whichever value is on disk for
    // this bot.
    const keySection = pubkey
      ? `<div style="display:flex;gap:6px;align-items:center;margin-top:6px">
           <span style="font-size:0.75rem;color:var(--text2);flex-shrink:0">SSH key (this bot):</span>
           <code style="font-size:0.7rem;background:var(--bg2);padding:4px 8px;border-radius:4px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtml(pubkey)}</code>
           <button class="btn btn-ghost btn-sm" onclick="backupCopyKey('${escHtml(botId)}')" title="Copy public key">Copy</button>
         </div>`
      : `<div style="margin-top:6px">
           <button class="btn btn-ghost btn-sm" id="backup-genkey-btn-${escHtml(botId)}" onclick="backupGenerateKey('${escHtml(botId)}')">Generate SSH key (per-bot)</button>
           <span style="font-size:0.75rem;color:var(--text3);margin-left:8px">Or use "Distribute one key" above to share one key across all bots.</span>
         </div>`;

    const drifted = Array.isArray(bs.drifted_keys) ? bs.drifted_keys : [];
    const driftSection = drifted.length
      ? `<div style="margin-top:10px;padding:8px 10px;background:var(--bg2);border-left:3px solid var(--warning);border-radius:4px">
           <div style="font-size:0.8rem;color:var(--warning);margin-bottom:4px">⚠ Config drift — ${drifted.length} key(s) differ from baseline</div>
           <div style="font-size:0.78rem;color:var(--text2);margin-bottom:6px">${drifted.map(k => `<code>${escHtml(k)}</code>`).join(', ')}</div>
           <button class="btn btn-ghost btn-sm" id="backup-accept-drift-btn-${escHtml(botId)}" onclick="backupAcceptDrift('${escHtml(botId)}')">Accept as baseline</button>
         </div>`
      : '';

    // Guided-setup CTA — only when the bot has no backupRepoUrl yet.
    // Opens the existing `openOnboardModal('github', [botId])` flow,
    // which handles PAT auto-discovery, repo creation, deploy-key
    // registration in one shot. The plain URL input below is kept as the
    // power-user escape hatch (paste a URL someone else already provisioned).
    const guidedSetupCTA = configured ? '' : `<div style="margin-top:10px;padding:10px 12px;background:rgba(99,102,241,0.06);border-left:3px solid var(--accent, #6366f1);border-radius:4px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <div style="flex:1;min-width:200px;font-size:0.82rem;color:var(--text2)">
          <strong>No backup configured.</strong> Set up a private GitHub repo + deploy key in one guided flow — no SSH-key manual copy needed.
        </div>
        <button class="btn btn-primary btn-sm" onclick="openOnboardModal('github',['${escHtml(botId)}'])">Set up GitHub backup →</button>
      </div>`;

    // Phase 4d: per-bot preflight slot — populated by loadBackupPreflight after
    // initial render so the operator sees "Next ~X MB / Y files" inline with
    // the last-backup badge. Skipped for unconfigured bots — no destination
    // means no estimate matters.
    const preflightSlot = configured && !bs.needs_init
      ? `<span style="color:var(--text3)">·</span><span id="backup-preflight-${escHtml(botId)}" style="color:var(--text2)"><span class="subtle">estimating…</span></span>`
      : '';

    return `<div class="card" style="margin-bottom:6px;padding:10px 12px" id="backup-card-${escHtml(botId)}">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap">
        <span style="font-weight:600">${escHtml(botLabel(botId))}</span>
        <div style="display:flex;align-items:center;gap:8px;font-size:0.78rem">
          <span>${lastBackup}</span>
          ${preflightSlot}
          ${backupNowBtn}
        </div>
      </div>
      ${guidedSetupCTA}
      <div style="display:flex;gap:6px;align-items:center;margin-top:6px">
        <input id="backup-url-${escHtml(botId)}" type="text" class="input" style="flex:1;font-size:0.82rem;font-family:monospace"
          placeholder="git@github.com:${escHtml(defaultAccount)}/${escHtml(botId)}-workspace.git"
          value="${escHtml(url)}">
        <button class="btn btn-primary btn-sm" onclick="backupSaveUrl('${escHtml(botId)}')">Save</button>
      </div>
      ${driftSection}
      ${keySection}
    </div>`;
  }).join('');

  el.innerHTML = sharedKeyCard + (cards || '<div class="empty">No bots configured.</div>');

  // Fan out preflight estimates after the cards land in the DOM. Each call
  // populates its own slot independently so a slow / missing bot doesn't
  // hold up the others. Errors render inline ("—") rather than alerting.
  for (const botId of orderedBotIds(bots)) {
    const info = bots[botId] || {};
    const bs = (statusBots[botId] || {});
    if ((info.backupRepoUrl || '').trim() && !bs.needs_init) {
      loadBackupPreflight(botId);
    }
  }
}

// Phase 4d: fetch + render the per-bot preflight estimate. Populates the
// matching `#backup-preflight-<botId>` slot rendered by renderBackupConfig.
async function loadBackupPreflight(botId) {
  const slot = document.getElementById(`backup-preflight-${botId}`);
  if (!slot) return;
  const data = await api('GET', `/api/backup/cloud/preflight?botId=${encodeURIComponent(botId)}`);
  if (!data || data.error || !data.by_classification) {
    slot.innerHTML = '<span class="subtle">—</span>';
    return;
  }
  const cloud = data.by_classification.cloud || {files: 0, bytes: 0};
  const local = data.by_classification.local || {files: 0, bytes: 0};
  const ephem = data.by_classification.ephemeral || {files: 0, bytes: 0};
  const tildePrefix = data.truncated ? '~' : '';
  const cloudBytes = _formatBackupBytes(cloud.bytes);
  const tooltip = `Cloud: ${cloud.files} files / ${_formatBackupBytes(cloud.bytes)}
Local (not pushed): ${local.files} files / ${_formatBackupBytes(local.bytes)}
Ephemeral (not pushed): ${ephem.files} files / ${_formatBackupBytes(ephem.bytes)}${data.truncated ? '\n(Cap of 100k files hit — totals are estimates.)' : ''}`;
  // Show counts for whatever's non-zero — operators rarely care about
  // ephemeral, but the tooltip carries the full breakdown either way.
  const excludedNote = (local.files + ephem.files) > 0
    ? ` <span class="subtle">· ${local.files + ephem.files} excluded</span>`
    : '';
  slot.innerHTML = `<span title="${escHtml(tooltip)}">Next ${tildePrefix}${escHtml(cloudBytes)} / ${tildePrefix}${cloud.files} file${cloud.files === 1 ? '' : 's'}${excludedNote}</span>`;
}

function _formatBackupBytes(n) {
  if (!n) return '0 B';
  const k = 1024;
  if (n < k)        return `${n} B`;
  if (n < k * k)    return `${(n / k).toFixed(1)} KB`;
  if (n < k * k * k) return `${(n / (k * k)).toFixed(1)} MB`;
  return `${(n / (k * k * k)).toFixed(2)} GB`;
}

async function backupSaveUrl(botId) {
  const input = document.getElementById(`backup-url-${botId}`);
  if (!input) return;
  const url = input.value.trim();
  const d = await api('PATCH', '/api/backup/cloud/config', {botId, backupRepoUrl: url});
  if (d?.ok) {
    _showToast(`Backup URL ${url ? 'saved' : 'cleared'} for ${botId}`, 'success');
    _backupCfgData = null;
    loadBackupConfig();
  } else {
    _showToast(`Failed: ${d?.error || 'unknown error'}`, 'error');
  }
}

async function backupGenerateKey(botId) {
  const btn = document.getElementById(`backup-genkey-btn-${botId}`);
  if (btn) { btn.disabled = true; btn.textContent = 'Generating…'; }
  const d = await api('POST', '/api/backup/cloud/keys/generate', {botId});
  if (d?.ok) {
    _backupCfgData = null;
    await loadBackupConfig();
  } else {
    // Re-query: DOM may have been replaced by a concurrent loadBackupConfig call
    const b = document.getElementById(`backup-genkey-btn-${botId}`);
    if (b) { b.disabled = false; b.textContent = 'Generate SSH key'; }
    _showToast(`Failed: ${d?.error || 'unknown error'}`, 'error');
  }
}

async function backupAcceptDrift(botId) {
  const btn = document.getElementById(`backup-accept-drift-btn-${botId}`);
  // Re-fetch fresh drift detail so the confirm shows exactly what's about to
  // be committed — the cached card may be stale by the time the user clicks.
  const detail = await api('GET', `/api/security/drift-detail?botId=${encodeURIComponent(botId)}`);
  const keys = (detail && Array.isArray(detail.drifted_keys)) ? detail.drifted_keys : [];
  if (!keys.length) {
    _showToast(`No drift detected for ${botId} — already at baseline`, 'info');
    _backupCfgData = null;
    await loadBackupConfig();
    return;
  }
  const ok = await confirmModal({body: (
    `Accept current openclaw.json as the new baseline for ${botId}?\n\n` +
    `Keys that will be accepted: ${keys.join(', ')}\n\n` +
    `Heal will stop reporting drift on these keys until the next change.`
  )});
  if (!ok) return;
  if (btn) { btn.disabled = true; btn.textContent = 'Committing…'; }
  const r = await api('POST', '/api/security/accept-drift', {botId});
  if (r?.ok) {
    _showToast(`Baseline updated for ${botId}: ${r.message || 'committed'}`, 'success');
    _backupCfgData = null;
    await loadBackupConfig();
  } else {
    const b = document.getElementById(`backup-accept-drift-btn-${botId}`);
    if (b) { b.disabled = false; b.textContent = 'Accept as baseline'; }
    _showToast(`Failed: ${r?.error || 'unknown error'}`, 'error');
  }
}

async function backupInitWorkspace(botId) {
  const btn = document.getElementById(`backup-init-btn-${botId}`);
  if (btn) { btn.disabled = true; btn.textContent = 'Initializing…'; }
  const d = await api('POST', '/api/backup/cloud/init', {botId});
  if (d?.ok) {
    _showToast(`Workspace ${d.status === 'already-initialized' ? 'already initialized' : 'initialized'} for ${botId}`, 'success');
    _backupCfgData = null;
    await loadBackupConfig();
  } else {
    const b = document.getElementById(`backup-init-btn-${botId}`);
    if (b) { b.disabled = false; b.textContent = 'Init now'; }
    _showToast(`Failed: ${d?.error || d?.status || 'unknown error'}`, 'error');
  }
}

function backupCopyKey(botId) {
  const bots = _backupCfgData?.bots || {};
  const pubkey = bots[botId]?.pubkey || '';
  if (!pubkey) return;
  navigator.clipboard.writeText(pubkey).then(() => _showToast('Public key copied', 'success'));
}

// Classify a per-bot `github_status` string into a structured
// guidance card. Mirrors packages/analyzer/backup_diagnostics.classify
// on the admin-server side: the operator sees a "Likely cause" line
// + numbered fix steps for every recognisable failure pattern, rather
// than a raw HTTP error glued behind a red ✗.
//
// Returns null when the status is "registered" / "already_present" /
// the generic skipped-* values that already self-explain — in those
// cases the chip row is enough on its own.
//
// fix_steps entries are TRUSTED HTML (they can include <a>, <code>,
// <b>) — never feed user-controlled data into them. The classifier
// patterns match on shape (status code + message phrase) so they work
// against both the per-repo deploy-key POST path and the post-#2366
// per-user POST /user/keys path; the operator-visible error string
// passes through escHtml in the chip label above the card.
function classifyDistributeKeyError(githubStatus) {
  if (!githubStatus) return null;
  const gs = String(githubStatus);

  if (!gs.startsWith('failed:') && gs !== 'skipped:unparseable_url') return null;

  // "key is already in use" — happens when the same pubkey is currently
  // bound as a deploy key on some other repo. GitHub refuses to
  // register it on the user account until that deploy key is revoked.
  // The conflicting-repo discovery (GET /user/keys + per-repo deploy-key
  // scan) is deferred as expensive; for now the operator gets a clear
  // explanation + a link to their keys page.
  if (/\b422\b.*key is already in use/i.test(gs)) {
    return {
      cause_id: 'key_already_in_use',
      title: 'Pubkey already registered as a deploy key on another repo',
      summary: 'GitHub user-account keys cannot duplicate a public key that\'s already bound as a deploy key. Until the conflicting deploy key is revoked, user-account registration fails.',
      fix_steps: [
        'Open <a href="https://github.com/settings/keys" target="_blank" rel="noopener">github.com/settings/keys</a> and check whether the pubkey shown above is already listed. If it is, the user-account registration succeeded earlier — you can ignore this row.',
        'Otherwise the pubkey is bound as a per-repo deploy key elsewhere. Find that repo (any repo you own with this pubkey under <b>Settings → Deploy keys</b>), revoke the entry, then re-run <b>Generate + distribute</b>.',
        'Alternative: leave the per-repo deploy key in place — the bot\'s backup still works through it. The shared-key model is a convenience, not a requirement.',
      ],
      severity: 'red',
    };
  }

  // 401 Bad credentials — PAT missing/expired/revoked.
  if (/\b401\b/i.test(gs)) {
    return {
      cause_id: 'pat_unauthorized',
      title: 'GitHub rejected the PAT (401 Bad credentials)',
      summary: 'The pod-wide GitHub PAT (keystore key <code>github_pat</code>) is missing, expired, or revoked.',
      fix_steps: [
        'Open <a href="https://github.com/settings/tokens" target="_blank" rel="noopener">github.com/settings/tokens</a> and verify the token still exists and hasn\'t expired.',
        'If expired, generate a new classic PAT with the <code>admin:public_key</code> scope (and <code>repo</code> if Evolve creates backup repos for you), then store it by re-running the <b>Backup → Cloud</b> wizard (it persists the PAT to the keystore — hand-editing network.json no longer works).',
        'Re-run <b>Generate + distribute</b>.',
      ],
      severity: 'red',
    };
  }

  // 403 — usually PAT scope or org policy.
  if (/\b403\b/i.test(gs)) {
    return {
      cause_id: 'pat_forbidden',
      title: 'GitHub refused the request (403 — PAT scope or org policy)',
      summary: 'The PAT was recognised but the request was forbidden. Most often the token lacks <code>admin:public_key</code>, or an org policy blocks user-key creation.',
      fix_steps: [
        'Open <a href="https://github.com/settings/tokens" target="_blank" rel="noopener">github.com/settings/tokens</a> and confirm the token has the <code>admin:public_key</code> scope (classic) or "SSH keys → Read and write" (fine-grained).',
        'If your org restricts user-key creation, an org admin must allow it — or fall back to adding the pubkey as a per-repo deploy key (use the manual-fallback block below).',
        'Re-run <b>Generate + distribute</b> after fixing the PAT.',
      ],
      severity: 'red',
    };
  }

  // 404 — repo missing or PAT cannot see it.
  if (/\b404\b/i.test(gs)) {
    return {
      cause_id: 'repo_not_visible',
      title: 'GitHub returned 404 — repo missing or PAT cannot see it',
      summary: 'Either the bot\'s <code>backupRepoUrl</code> is wrong, the repo was renamed or transferred, or the PAT doesn\'t have access.',
      fix_steps: [
        'Verify the URL on this bot\'s Backup → Cloud card matches a repo you can browse on GitHub.',
        'If the repo lives under an org, confirm the PAT has access (classic: <code>repo</code> scope with SSO authorized; fine-grained: per-repo permission grant).',
      ],
      severity: 'red',
    };
  }

  // Generic 422 (validation other than key-in-use).
  if (/\b422\b/i.test(gs)) {
    return {
      cause_id: 'github_validation',
      title: 'GitHub rejected the request (422 validation)',
      summary: 'GitHub refused with a validation error. The raw message is in the row label above — read it for the specific field that\'s wrong.',
      fix_steps: [
        'If the message mentions an invalid public key, the pubkey on disk may be malformed; re-run <b>Generate + distribute</b> to regenerate it.',
        'If unclear, add the pubkey manually via <a href="https://github.com/settings/keys" target="_blank" rel="noopener">github.com/settings/keys</a> as a fallback (the manual block at the bottom of this panel has the value).',
      ],
      severity: 'red',
    };
  }

  // skipped:unparseable_url — currently no operator hint.
  if (gs === 'skipped:unparseable_url') {
    return {
      cause_id: 'unparseable_url',
      title: 'This bot\'s backupRepoUrl couldn\'t be parsed',
      summary: 'The shared key was written to disk but GitHub registration was skipped because the URL doesn\'t parse as a github.com remote.',
      fix_steps: [
        'Open this bot\'s Backup → Cloud card and check the <code>backupRepoUrl</code> field.',
        'Expected formats: <code>git@github.com:org/repo.git</code> (SSH) or <code>https://github.com/org/repo.git</code> (HTTPS — converted automatically). Anything else is rejected.',
      ],
      severity: 'orange',
    };
  }

  // Any other failed:* — generic catch-all so operator always sees
  // SOMETHING actionable. Keeps the manual-fallback path visible.
  if (gs.startsWith('failed:')) {
    return {
      cause_id: 'github_unknown_failure',
      title: 'GitHub registration failed',
      summary: 'GitHub returned an error the classifier didn\'t recognise. The raw message is in the row label above.',
      fix_steps: [
        'Read the raw error — it usually names the field or scope.',
        'Add the pubkey manually at <a href="https://github.com/settings/keys" target="_blank" rel="noopener">github.com/settings/keys</a> as a fallback.',
      ],
      severity: 'red',
    };
  }

  return null;
}

// Render a guidance card for a single classified cause. Returned HTML
// is inserted as the inline expansion under a failure row in the
// distribute-key result grid. Visual treatment mirrors the cause
// block in renderBackupDiagnosticPanel — keep the two consistent.
function renderDistributeKeyGuidanceCard(cause) {
  if (!cause) return '';
  const accent = cause.severity === 'orange' ? '#ffa502' : '#ff4757';
  const bg = cause.severity === 'orange' ? 'rgba(255,165,2,0.08)' : 'rgba(255,71,87,0.08)';
  // fix_steps entries are TRUSTED HTML (classifier-authored, not
  // user-controlled). title and summary are author-controlled plain
  // text; rendered without escHtml so the inline <code>/<b> tags in
  // summary stay live. Bot id never reaches this function — caller
  // identifies the affected bot in the row chip above.
  return `<div style="margin:6px 0 10px 88px;padding:10px 12px;background:${bg};border-left:3px solid ${accent};border-radius:4px;max-width:760px">
      <div style="font-size:0.8rem;font-weight:500;color:var(--text);margin-bottom:6px">
        Likely cause: ${cause.title}
      </div>
      <div style="font-size:0.76rem;color:var(--text2);margin-bottom:8px;line-height:1.5">
        ${cause.summary}
      </div>
      <ol style="margin:0;padding-left:20px;font-size:0.76rem;color:var(--text);line-height:1.55">
        ${(cause.fix_steps || []).map(s => `<li style="margin-bottom:4px">${s}</li>`).join('')}
      </ol>
    </div>`;
}

// Generate one private key + distribute a copy into every configured
// bot's ~/.ssh/evolve-backup-<bot>. After this, every per-bot card
// below shows the SAME pubkey value — that's the visual cue that one
// key now covers the whole pod. The operator copies the pubkey ONCE
// into their GitHub user account and every repo push works.
async function backupDistributeKey() {
  const btn = document.getElementById('backup-distribute-btn');
  const status = document.getElementById('backup-distribute-status');
  const result = document.getElementById('backup-distribute-result');
  if (btn) { btn.disabled = true; btn.textContent = 'Distributing…'; }
  if (status) status.textContent = 'Generating + copying to each bot…';
  if (result) result.innerHTML = '';
  const r = await api('POST', '/api/backup/cloud/keys/distribute', { generate: true });
  if (btn) { btn.disabled = false; btn.textContent = 'Generate + distribute'; }
  if (!r || !r.ok) {
    if (status) status.textContent = '';
    if (result) result.innerHTML = `<div style="color:var(--red);font-size:0.78rem">Failed: ${escHtml((r && r.error) || 'unknown')}</div>`;
    return;
  }
  const results = r.results || [];
  const ok = results.filter(x => x.status === 'distributed').length;
  const fail = results.filter(x => x.status === 'failed').length;
  // GitHub auto-registration buckets — fields added 2026-06-07 so the
  // operator no longer has to manually re-add the pubkey on every
  // repo. Pre-fix: the card said "register this pubkey ONCE on your
  // GitHub user account" and the operator had to follow up; the
  // wizard's later "✓ reused" check then reported false positives
  // against the OLD per-bot keys still on GitHub.
  const ghRegistered = results.filter(x => x.github_status === 'registered').length;
  const ghAlready = results.filter(x => x.github_status === 'already_present').length;
  const ghFailed = results.filter(x => x.github_status && x.github_status.startsWith('failed:'));
  const ghSkipped = results.filter(x => x.github_status && x.github_status.startsWith('skipped:'));
  const patAvailable = !!r.pat_available;

  if (status) {
    let line = `Distributed to ${ok} bot${ok === 1 ? '' : 's'}${fail ? ` (${fail} failed)` : ''}.`;
    if (patAvailable) {
      const regParts = [];
      if (ghRegistered) regParts.push(`${ghRegistered} newly registered on GitHub`);
      if (ghAlready) regParts.push(`${ghAlready} already present`);
      if (ghFailed.length) regParts.push(`${ghFailed.length} registration failed`);
      if (regParts.length) line += ' ' + regParts.join(' · ') + '.';
    }
    status.textContent = line;
  }
  if (result) {
    // Per-bot registration status, rendered as a small grid. Only
    // shown when something useful exists (any per-bot status field).
    // Failure rows get an inline guidance card from
    // classifyDistributeKeyError so the operator sees a "Likely cause"
    // + fix steps inline instead of just a red HTTP error.
    const showGhGrid = results.some(x => x.github_status);
    const ghGridRows = showGhGrid ? results.map(x => {
      const gs = x.github_status || '';
      let colour = 'var(--text3)', glyph = '·', label = gs;
      if (gs === 'registered') { colour = 'var(--green)'; glyph = '✓'; label = 'registered on GitHub'; }
      else if (gs === 'already_present') { colour = 'var(--green)'; glyph = '✓'; label = 'already on GitHub'; }
      else if (gs.startsWith('failed:')) { colour = 'var(--red)'; glyph = '✗'; label = gs.slice(7); }
      else if (gs === 'skipped:no_url') { colour = 'var(--text3)'; glyph = '⊘'; label = 'no backup repo configured'; }
      else if (gs === 'skipped:no_pat') { colour = 'var(--yellow)'; glyph = '⚠'; label = 'no PAT — add manually'; }
      else if (gs === 'skipped:distribute_failed') { colour = 'var(--text3)'; glyph = '⊘'; label = 'distribute failed'; }
      else if (gs === 'skipped:unparseable_url') { colour = 'var(--yellow)'; glyph = '⚠'; label = 'unparseable backup URL'; }
      const cause = classifyDistributeKeyError(gs);
      const guidance = cause ? renderDistributeKeyGuidanceCard(cause) : '';
      return `<div style="display:flex;gap:8px;font-size:0.74rem">
        <code style="color:var(--text2);min-width:80px">${escHtml(botLabel(x.bot_id))}</code>
        <span style="color:${colour}">${glyph} ${escHtml(label)}</span>
      </div>${guidance}`;
    }).join('') : '';

    const manualFallback = (!patAvailable || ghSkipped.length || ghFailed.length) ? `
      <div style="margin-top:10px;padding:8px 10px;background:rgba(255,165,2,0.08);border-left:3px solid var(--yellow);border-radius:4px">
        <div style="font-size:0.78rem;color:var(--text);margin-bottom:6px">
          ${patAvailable
            ? '⚠ Some bots could not be auto-registered. Add this pubkey manually to their repo Settings → Deploy keys (or to your GitHub user account):'
            : '⚠ No GitHub PAT configured — register this pubkey ONCE on your GitHub user account (Settings → SSH and GPG keys → New SSH key) so every backup repo authenticates with the same key:'}
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <code style="font-size:0.7rem;background:var(--bg2);padding:4px 8px;border-radius:4px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtml(r.pubkey || '')}</code>
          <button class="btn btn-ghost btn-sm" onclick="navigator.clipboard.writeText('${escHtml(r.pubkey || '')}').then(()=>toast('Copied','ok'))">Copy</button>
        </div>
      </div>` : '';

    result.innerHTML = `
      ${showGhGrid ? `<div style="display:flex;flex-direction:column;gap:4px;margin-bottom:6px">${ghGridRows}</div>` : ''}
      ${manualFallback}
    `;
  }
  _backupCfgData = null;
  await loadBackupConfig();
}

// Per-bot push-test probe — opt-in follow-up to Distribute Key. Runs
// `git ls-remote` against each bot's backupRepoUrl using the canonical
// shared SSH key. Catches the post-distribute failure mode where
// user-key registration silently succeeds but the SSH identity can't
// actually reach the repo (e.g. org-collaborator-read-no-push for a
// bot whose repo lives under an org the PAT user can't push to).
//
// Server endpoint: POST /api/backup/cloud/keys/push-test → per-bot
// {status: ok|failed|skipped:no_url, stderr?}. The probe is one SSH
// handshake per bot — slow but bounded; render an in-flight spinner
// so the operator doesn't think the page froze.
async function backupRunPushTest() {
  const btn = document.getElementById('backup-pushtest-btn');
  const result = document.getElementById('backup-pushtest-result');
  if (btn) { btn.disabled = true; btn.textContent = 'Probing…'; }
  if (result) result.innerHTML = `<div style="font-size:0.78rem;color:var(--text2)"><span class="spinner"></span> Running git ls-remote per bot — usually 5–20s total.</div>`;
  const r = await api('POST', '/api/backup/cloud/keys/push-test', {});
  if (btn) { btn.disabled = false; btn.textContent = 'Run push test'; }
  if (!result) return;
  if (!r || !r.ok) {
    result.innerHTML = `<div style="color:var(--red);font-size:0.78rem">Push test failed: ${escHtml((r && r.error) || 'unknown')}</div>`;
    return;
  }
  const results = r.results || [];
  if (!results.length) {
    result.innerHTML = `<div style="font-size:0.78rem;color:var(--text3)">No bots had a backup repo configured.</div>`;
    return;
  }
  const okCount = results.filter(x => x.status === 'ok').length;
  const failCount = results.filter(x => x.status === 'failed').length;
  const skipCount = results.filter(x => x.status && x.status.startsWith('skipped:')).length;
  const summary = `Probed ${results.length} bot${results.length === 1 ? '' : 's'}: <span style="color:var(--green)">${okCount} ok</span>${failCount ? ` · <span style="color:var(--red)">${failCount} failed</span>` : ''}${skipCount ? ` · <span style="color:var(--text3)">${skipCount} skipped</span>` : ''}.`;
  const rows = results.map(x => {
    let colour = 'var(--text3)', glyph = '·', label = x.status || '';
    if (x.status === 'ok') { colour = 'var(--green)'; glyph = '✓'; label = 'reaches GitHub (SSH auth + read access)'; }
    else if (x.status === 'failed') { colour = 'var(--red)'; glyph = '✗'; label = 'failed'; }
    else if (x.status === 'skipped:no_url') { colour = 'var(--text3)'; glyph = '⊘'; label = 'no backup repo configured'; }
    const stderr = (x.status === 'failed' && x.stderr) ? `
      <pre style="margin:6px 0 10px 88px;padding:8px 10px;background:var(--bg2);border:1px solid var(--border);border-radius:4px;font-size:0.72rem;color:var(--text2);overflow-x:auto;white-space:pre-wrap;word-break:break-word;max-width:760px">${escHtml(x.stderr)}</pre>` : '';
    return `<div style="display:flex;gap:8px;font-size:0.74rem">
      <code style="color:var(--text2);min-width:80px">${escHtml(botLabel(x.bot_id))}</code>
      <span style="color:${colour}">${glyph} ${escHtml(label)}</span>
    </div>${stderr}`;
  }).join('');
  // Footer note explains the probe's limit: ls-remote confirms identity +
  // read, not write. A green row doesn't prove `git push` will succeed
  // — the next nightly backup attempt is the real verifier.
  const footer = `<div style="margin-top:8px;font-size:0.72rem;color:var(--text3);line-height:1.45">
    Note: this runs <code>git ls-remote</code>, which confirms the SSH identity reaches
    the repo. It does <b>not</b> prove write access — a successful row means the next
    backup push will reach GitHub, not that GitHub will accept the push.
  </div>`;
  result.innerHTML = `
    <div style="font-size:0.78rem;color:var(--text);margin-bottom:6px">${summary}</div>
    <div style="display:flex;flex-direction:column;gap:4px">${rows}</div>
    ${footer}
  `;
}

// On-demand backup — kicks the nightly job for one bot without waiting for
// 2 AM. Talks to /api/backup/cloud/run which runs backup.py --bot <id>
// synchronously (per-bot backups are typically a few-second push).
async function backupNow(botId) {
  const btn = document.getElementById(`backup-now-btn-${botId}`);
  if (btn) { btn.disabled = true; btn.textContent = 'Backing up…'; }
  const r = await api('POST', '/api/backup/cloud/run', {botId});
  if (r && r.ok) {
    const status = (r.results && r.results[0] && r.results[0].status) || 'ok';
    const ok = status === 'ok' || status === 'no-changes';
    _showToast(`Backup ${ok ? 'completed' : 'returned: ' + status} for ${botId}`, ok ? 'success' : 'info');
    _backupCfgData = null;
    await loadBackupConfig();
  } else {
    const b = document.getElementById(`backup-now-btn-${botId}`);
    if (b) { b.disabled = false; b.textContent = 'Backup now'; }
    _showToast(`Backup failed: ${(r && r.error) || 'unknown error'}`, 'error');
  }
}

// Pod-wide on-demand backup — iterates every bot with backupRepoUrl set.
// Can take a while (each bot is a network push), so disable the button
// and stream status into the toolbar while we wait.
async function backupNowAll() {
  const btn = document.getElementById('backup-now-all-btn');
  const statusEl = document.getElementById('backup-now-all-status');
  if (btn) { btn.disabled = true; btn.textContent = 'Backing up…'; }
  if (statusEl) statusEl.textContent = 'Running — this can take ~10s per bot…';
  const r = await api('POST', '/api/backup/cloud/run', {});
  if (btn) { btn.disabled = false; btn.textContent = 'Backup all bots now'; }
  if (r && r.ok) {
    const results = r.results || [];
    const ok = results.filter(x => x.status === 'ok' || x.status === 'no-changes').length;
    const fail = results.filter(x => x.status === 'failed').length;
    const skip = results.filter(x => x.status === 'no-url' || x.status === 'skipped').length;
    if (statusEl) statusEl.textContent = `${ok} backed up · ${skip} skipped · ${fail} failed`;
    _showToast(`Backup pass: ${ok} ok, ${skip} skipped, ${fail} failed`, fail ? 'error' : 'success');
    _backupCfgData = null;
    await loadBackupConfig();
  } else {
    if (statusEl) statusEl.textContent = '';
    _showToast(`Backup-all failed: ${(r && r.error) || 'unknown error'}`, 'error');
  }
}

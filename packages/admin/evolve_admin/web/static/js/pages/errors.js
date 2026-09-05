// ════════════════════════════════════════════════════════════════════════
// Page: Errors
//
// Two related surfaces ship from this module:
//
//   1. The global error-log drawer — opened from anywhere via
//      openErrorLog(), bound to /api/health-style failures via
//      logError() (the same logError that core/api.js's api() calls as
//      a free-variable reference at runtime).
//
//   2. The standalone Errors page — loadErrors() + the _renderErrorsX
//      family + the submit-upstream / preflight flow.
//
// State (let _errorLog, _errData, _errFiltered, _errOpenDetail,
// _errPendingSubmit, _errCachedRepo) lives in script-scope at the top
// of this file. Because top-level let is shared across all classic
// <script> tags on the page, anything in the main inline script can
// still mutate _errorLog (only logError + clearErrorLog do today, and
// both live here).
//
// Loaded alongside the core/ modules and before the main inline
// <script>; api.js's logError() lookup resolves at call time.
// ════════════════════════════════════════════════════════════════════════

// Moved from the main inline script's state-globals block — kept here
// alongside its only mutators (logError + clearErrorLog).
let _errorLog = [];

// ══════════════════════════════════════════════════════
// Error Log
// ══════════════════════════════════════════════════════
function logError(source, msg, detail) {
  const entry = { ts: new Date().toISOString(), source: String(source||'?'), msg: String(msg||''), detail: String(detail||'') };
  _errorLog.unshift(entry);
  const btn = document.getElementById('errlog-btn');
  const cnt = document.getElementById('errlog-count');
  if (btn) btn.classList.add('has-errors');
  if (cnt) cnt.textContent = _errorLog.length;
  _renderErrorLog();
  // Returning the entry lets callers attach diagnostic annotations
  // asynchronously (e.g. api()'s network-error path probes /api/health
  // and stamps server_up + uptime_seconds onto the entry once the probe
  // resolves). Old callsites that ignore the return value still work
  // unchanged.
  return entry;
}

// Probe /api/health and stamp the result onto an error-log entry so
// future fetch-failure triage can tell "server bouncing" from "server
// up, chat endpoint specifically broke". Fire-and-forget; never throws.
//
// Common case: a kickstart of the admin-ui daemon races a chat send.
// The fetch fails with `Load failed` (Safari) or `Failed to fetch`
// (Chrome), api() surfaces the network_error flag, the operator sees
// the friendly retry bubble. WITHOUT this probe, the error log says
// only "Load failed" — no way to distinguish "daemon was down" from
// "chat route specifically broken". With this probe, the entry gains
// `server_up: false` (or `true` + low uptime_seconds = "just
// restarted, race window") and the cause is self-evident.
//
// The probe itself can also fail (e.g. true network outage); in that
// case the entry gets server_up:false with a probe_error field.
async function _probeServerForErrorLog(entry) {
  if (!entry) return;
  try {
    const r = await fetch('/api/health', { method: 'GET', cache: 'no-store' });
    if (r.ok) {
      let data = {};
      try { data = await r.json(); } catch (_) {}
      entry.server_up = true;
      if (typeof data.uptime_seconds === 'number') {
        entry.server_uptime_s = data.uptime_seconds;
      }
    } else {
      entry.server_up = false;
      entry.server_probe_status = r.status;
    }
  } catch (probeErr) {
    entry.server_up = false;
    entry.server_probe_error = String(probeErr && probeErr.message || probeErr);
  }
  // Re-render so the operator sees the annotation if the error log
  // drawer is open. Harmless if it isn't.
  if (typeof _renderErrorLog === 'function') {
    try { _renderErrorLog(); } catch (_) {}
  }
}
function _renderErrorLog() {
  const list = document.getElementById('errlog-list');
  if (!list) return;
  if (!_errorLog.length) {
    list.innerHTML = '<div class="errlog-empty">No errors logged this session.</div>';
    return;
  }
  list.innerHTML = _errorLog.map((e, i) => {
    // Optional server-up annotation populated by _probeServerForErrorLog
    // when api()'s fetch fails. If absent, the entry pre-dates the probe
    // (or the probe is still in flight). Renders as a compact one-line
    // diagnostic just above the detail.
    let serverUpHtml = '';
    if (e.server_up === true) {
      const uptime = (typeof e.server_uptime_s === 'number')
        ? ` (uptime ${e.server_uptime_s}s${e.server_uptime_s < 30 ? ' — just restarted' : ''})`
        : '';
      serverUpHtml = `<div class="errlog-server-up" style="font-size:0.78rem;color:var(--text2);margin-top:2px">server reachable at probe time${uptime}</div>`;
    } else if (e.server_up === false) {
      const why = e.server_probe_status
        ? ` (/api/health → ${e.server_probe_status})`
        : (e.server_probe_error ? ` (probe error: ${escHtml(e.server_probe_error)})` : '');
      serverUpHtml = `<div class="errlog-server-up" style="font-size:0.78rem;color:var(--red);margin-top:2px">server NOT reachable at probe time${why} — likely a daemon restart race or network blip</div>`;
    }
    return `
    <div class="errlog-entry">
      <div class="errlog-meta">
        <span class="errlog-ts">${e.ts.replace('T',' ').slice(0,19)}</span>
        <span class="errlog-src">[${escHtml(e.source)}]</span>
        <button class="errlog-copy" onclick="copyErrEntry(${i})" title="Copy to clipboard">⧉</button>
      </div>
      <div class="errlog-msg">${escHtml(e.msg)}</div>
      ${serverUpHtml}
      ${e.detail ? `<div class="errlog-detail" onclick="this.classList.toggle('expanded')">${escHtml(e.detail)}</div>` : ''}
    </div>`;
  }).join('');
}
// Format the server-up annotation as a one-line suffix for clipboard /
// download exports. Returns empty string if the entry doesn't carry the
// annotation (older entry, or probe still in flight).
function _formatServerUpForCopy(e) {
  if (e.server_up === true) {
    const uptime = (typeof e.server_uptime_s === 'number')
      ? ` (uptime ${e.server_uptime_s}s${e.server_uptime_s < 30 ? ' — just restarted' : ''})`
      : '';
    return `[server-probe] reachable${uptime}`;
  }
  if (e.server_up === false) {
    const why = e.server_probe_status
      ? ` (/api/health → ${e.server_probe_status})`
      : (e.server_probe_error ? ` (probe error: ${e.server_probe_error})` : '');
    return `[server-probe] NOT reachable${why}`;
  }
  return '';
}
function copyErrEntry(i) {
  const e = _errorLog[i];
  if (!e) return;
  const serverLine = _formatServerUpForCopy(e);
  const body = `[${e.ts}] [${e.source}] ${e.msg}\n${serverLine ? serverLine + '\n' : ''}${e.detail}`;
  navigator.clipboard.writeText(body).then(() => toast('Copied', 'ok'));
}
function openErrorLog() {
  document.getElementById('errlog-drawer').classList.add('open');
  _renderErrorLog();
}
function closeErrorLog() { document.getElementById('errlog-drawer').classList.remove('open'); }
function clearErrorLog() {
  _errorLog = [];
  const btn = document.getElementById('errlog-btn');
  const cnt = document.getElementById('errlog-count');
  if (btn) btn.classList.remove('has-errors');
  if (cnt) cnt.textContent = '0';
  _renderErrorLog();
}
function downloadErrorLog() {
  const txt = _errorLog.map(e => {
    const serverLine = _formatServerUpForCopy(e);
    return `[${e.ts}] [${e.source}] ${e.msg}\n${serverLine ? serverLine + '\n' : ''}${e.detail}`;
  }).join('\n\n');
  const a = document.createElement('a');
  a.href = 'data:text/plain;charset=utf-8,' + encodeURIComponent(txt || '(no errors)');
  a.download = `evolve-errors-${new Date().toISOString().slice(0,10)}.txt`;
  a.click();
}

// ══════════════════════════════════════════════════════
// Errors page (Phase 1D)
// ══════════════════════════════════════════════════════
//
// Data model: each entry has
//   { signature, title, severity, module, count, first_seen, last_seen,
//     status, sample, fingerprint, submitted_url? }
//
// Dedup is done server-side by /api/errors (one row per signature).
// Local status overrides are stored in localStorage keyed by signature.

let _errData = [];          // raw data from /api/errors
let _errFiltered = [];      // after filter
let _errOpenDetail = null;  // signature with open detail pane
let _errPendingSubmit = {}; // { signature, url } when pre-flight modal is open

function _errStatusKey(sig) { return 'evolve_err_status_' + sig; }
function _errGetStatus(sig) { return localStorage.getItem(_errStatusKey(sig)) || 'new'; }
function _errSetStatus(sig, status) { localStorage.setItem(_errStatusKey(sig), status); }
function _errGetSubmittedUrl(sig) { return localStorage.getItem('evolve_err_suburl_' + sig) || ''; }
function _errSetSubmittedUrl(sig, url) { localStorage.setItem('evolve_err_suburl_' + sig, url); }

async function loadErrors() {
  const tbody = document.getElementById('err-tbody');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="8" class="resp-table-fullspan"><div class="err-empty">Loading…</div></td></tr>';

  // Lookback dropdown controls the server-side ?hours scan window.
  // Default 168 (7d) when the select isn't yet on the page.
  const hours = parseInt(document.getElementById('err-filter-lookback')?.value || '168', 10);

  // Split fetch from render so a render-side ReferenceError doesn't masquerade
  // as "Failed to load" (PR #1671 — _relTime undef looked like an API failure).
  let d;
  try {
    const r = await fetch(`/api/errors?hours=${hours}`, { cache: 'no-store' });
    if (!r.ok) { tbody.innerHTML = `<tr><td colspan="8" class="resp-table-fullspan"><div class="err-empty">Could not load errors (HTTP ${r.status}).</div></td></tr>`; return; }
    d = await r.json();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8" class="resp-table-fullspan"><div class="err-empty">Failed to load: ${escHtml(String(e))}</div></td></tr>`;
    return;
  }
  _errData = d.errors || [];
  _errFiltered = _errData.slice();
  _renderErrorsSummary(d);
  _renderErrorsViewHint(d);
  _renderErrorsBadge();
  filterErrors();

  // Snapshot for the evo chat drawer's errors context pack. Captures
  // the same data the operator sees in the table (top N signatures
  // by recency, total occurrences, last-error timestamp) so evo can
  // answer "what's that error?" / "how many of these are new?" /
  // "what's the most recent failure?" with the same view the
  // operator has — no separate fetch needed.
  //
  // Spread ``_prev`` so any concurrent writer (none today, but the
  // pattern is mandatory per the 2026-05-20 snapshot-clobber bug
  // fixed in PR #1366) doesn't get clobbered. Future-proofs against
  // a polling refresh being added later.
  try {
    window._evoContextSnapshots = window._evoContextSnapshots || {};
    const top = (d.errors || []).slice(0, 10).map(e => ({
      signature: e.signature,
      title: (e.title || '').slice(0, 160),
      severity: e.severity || 'warn',
      module: e.module || null,
      count: e.count ?? 1,
      first_seen: e.first_seen || null,
      last_seen: e.last_seen || null,
      status: _errGetStatus(e.signature),
    }));
    const _prev = window._evoContextSnapshots.errors || {};
    window._evoContextSnapshots.errors = {
      ..._prev,
      cached_at: Math.floor(Date.now() / 1000),
      total_signatures: (d.errors || []).length,
      total_occurrences: d.total_occurrences ?? null,
      last_error_at: d.last_error_at || null,
      top,
    };
    if (typeof _evoDrawerUpdateContextChip === 'function') _evoDrawerUpdateContextChip();
  } catch (_) {}
}

function _renderErrorsSummary(d) {
  const total = document.getElementById('errors-total');
  const sigs = document.getElementById('errors-sigs');
  const lastAt = document.getElementById('errors-last-at');
  if (total) total.textContent = d.total_occurrences ?? d.errors?.reduce((s,e)=>s+(e.count||1),0) ?? '—';
  if (sigs) sigs.textContent = d.errors?.length ?? '—';
  if (lastAt) {
    const la = d.last_error_at;
    lastAt.textContent = la ? _relTime(new Date(la)) : 'none';
  }
}

function _renderErrorsViewHint(d) {
  // Shows a small banner when the server is filtering by a "Clear all"
  // cutoff, so the operator can tell why the table looks emptier than
  // they'd expect — and can wipe the cutoff with one click.
  const el = document.getElementById('err-view-hint');
  if (!el) return;
  const dismissed = d && d.view_dismissed_at;
  if (!dismissed) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  el.style.display = '';
  el.innerHTML = `Cleared ${escHtml(_relTime(new Date(dismissed)))} — only newer errors shown. <a onclick="errRestoreAll()" style="color:var(--accent);cursor:pointer;text-decoration:underline">Restore older</a>`;
}

async function errClearAll() {
  if (!await confirmModal({body: 'Clear all errors currently shown? The underlying log is untouched — new occurrences after now will still surface.', danger: true})) return;
  try {
    const r = await fetch('/api/errors/dismiss-all', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    if (!r.ok) { toast('Clear failed (HTTP ' + r.status + ')', 'err'); return; }
    toast('Errors cleared', 'ok');
    loadErrors();
  } catch (e) {
    toast('Clear failed: ' + String(e), 'err');
  }
}

async function errRestoreAll() {
  try {
    const r = await fetch('/api/errors/dismiss-all', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clear: true }),
    });
    if (!r.ok) { toast('Restore failed (HTTP ' + r.status + ')', 'err'); return; }
    toast('Restored full view', 'ok');
    loadErrors();
  } catch (e) {
    toast('Restore failed: ' + String(e), 'err');
  }
}

function _renderErrorsBadge() {
  const badge = document.getElementById('badge-errors');
  if (!badge) return;
  const newCount = _errData.filter(e => _errGetStatus(e.signature) === 'new').length;
  if (newCount > 0) {
    badge.textContent = newCount;
    badge.style.display = '';
  } else {
    badge.style.display = 'none';
  }
}

function filterErrors() {
  const sevFilter = (document.getElementById('err-filter-sev')?.value || '').toLowerCase();
  const statusFilter = (document.getElementById('err-filter-status')?.value || '').toLowerCase();
  const search = (document.getElementById('err-filter-search')?.value || '').toLowerCase();

  _errFiltered = _errData.filter(e => {
    if (sevFilter && e.severity !== sevFilter) return false;
    const status = _errGetStatus(e.signature);
    if (statusFilter && status !== statusFilter) return false;
    if (search) {
      const haystack = (e.title + ' ' + (e.module||'') + ' ' + (e.sample||'')).toLowerCase();
      if (!haystack.includes(search)) return false;
    }
    return true;
  });
  _renderErrorsTable();
}

function _renderErrorsTable() {
  const tbody = document.getElementById('err-tbody');
  if (!tbody) return;

  if (!_errFiltered.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="resp-table-fullspan"><div class="err-empty">No errors match the current filter.</div></td></tr>';
    return;
  }

  tbody.innerHTML = _errFiltered.map(e => {
    const status = _errGetStatus(e.signature);
    const sevCls = e.severity === 'alert' ? 'alert' : 'warn';
    const sevLabel = e.severity === 'alert' ? 'CRITICAL' : 'ERROR';
    const subUrl = _errGetSubmittedUrl(e.signature);
    const isOpen = _errOpenDetail === e.signature;
    // JSON.stringify produces a double-quoted JS string literal; embedding
    // that raw inside onclick="…" closes the attribute on its first `"`
    // and the handler never binds (every action button was inert until
    // we escape the quotes as `&quot;` so the attribute survives parsing).
    const sigAttr = escHtml(JSON.stringify(e.signature));
    return `
      <tr class="${isOpen ? 'err-detail-open' : ''}">
        <td data-label="Error" style="max-width:320px;word-break:break-word">
          <span style="font-size:0.78rem;color:var(--text)">${escHtml((e.title||'').slice(0,120))}</span>
        </td>
        <td data-label="Sev"><span class="err-sev-badge ${sevCls}">${sevLabel}</span></td>
        <td data-label="Module" style="font-size:0.75rem;color:var(--text3);max-width:140px;word-break:break-all">${escHtml(e.module||'—')}</td>
        <td data-label="Count" style="font-size:0.8rem;text-align:right">${e.count ?? 1}</td>
        <td data-label="First seen" style="font-size:0.74rem;color:var(--text3);white-space:nowrap">${e.first_seen ? _relTime(new Date(e.first_seen)) : '—'}</td>
        <td data-label="Last seen" style="font-size:0.74rem;color:var(--text3);white-space:nowrap">${e.last_seen ? _relTime(new Date(e.last_seen)) : '—'}</td>
        <td data-label="Status"><span class="err-status-badge ${status}">${status}</span></td>
        <td data-label="" class="err-actions-cell">
          <button class="err-action-btn" onclick="toggleErrDetail(${sigAttr})" title="${isOpen ? 'Hide detail' : 'View detail'}" style="display:inline-flex;align-items:center;gap:5px">
            <span class="expand-icon${isOpen ? ' is-open' : ''}" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>${isOpen ? 'Hide' : 'Detail'}
          </button>
          ${status !== 'acknowledged' && status !== 'submitted' && status !== 'wontfix'
            ? `<button class="err-action-btn" onclick="errAcknowledge(${sigAttr})" title="Mark as seen">Ack</button>`
            : ''}
          ${status !== 'submitted'
            ? `<button class="err-action-btn primary" onclick="errSubmitUpstream(${sigAttr})" title="Submit to GitHub">Submit ↗</button>`
            : subUrl
              ? `<a href="${escHtml(subUrl)}" target="_blank" rel="noopener" class="err-action-btn" style="display:inline-block;text-decoration:none">Issue ↗</a>`
              : '<span class="err-action-btn" style="opacity:0.5;cursor:default">Submitted</span>'}
        </td>
      </tr>
      <tr class="err-detail-row ${isOpen ? 'open' : ''}" id="err-detail-${CSS.escape(e.signature)}">
        <td colspan="8" class="resp-table-fullspan">
          <div class="err-detail-pane">
            <div style="margin-bottom:6px"><strong>Fingerprint:</strong> <code style="font-size:0.72rem">${escHtml(e.fingerprint||e.signature)}</code></div>
            ${e.module ? `<div style="margin-bottom:6px"><strong>Module:</strong> ${escHtml(e.module)}</div>` : ''}
            ${e.count > 1 ? `<div style="margin-bottom:6px"><strong>Occurrences:</strong> ${e.count} (first: ${e.first_seen||'?'}, last: ${e.last_seen||'?'})</div>` : ''}
            ${subUrl ? `<div style="margin-bottom:6px"><strong>GitHub issue:</strong> <a href="${escHtml(subUrl)}" target="_blank" rel="noopener" style="color:var(--accent)">${escHtml(subUrl)}</a></div>` : ''}
            <div style="margin-bottom:4px"><strong>Sample log line:</strong></div>
            <pre>${escHtml(e.sample||'(no sample)')}</pre>
          </div>
        </td>
      </tr>`;
  }).join('');
}

function toggleErrDetail(sig) {
  _errOpenDetail = _errOpenDetail === sig ? null : sig;
  _renderErrorsTable();
}

function errAcknowledge(sig) {
  _errSetStatus(sig, 'acknowledged');
  _renderErrorsBadge();
  _renderErrorsTable();
}

async function errSubmitUpstream(sig) {
  const entry = _errData.find(e => e.signature === sig);
  if (!entry) return;

  // Build search keywords from the title + fingerprint (strip timestamps/hex)
  const keywords = (entry.title || entry.fingerprint || sig)
    .replace(/[0-9a-f]{8,}/gi, '')   // strip hex IDs
    .replace(/\s+/g, ' ')
    .trim()
    .split(' ')
    .filter(Boolean)
    .slice(0, 6)
    .join('+');

  const modal = document.getElementById('err-preflight-modal');
  const titleEl = document.getElementById('err-preflight-title');
  const subEl = document.getElementById('err-preflight-sub');
  const bodyEl = document.getElementById('err-preflight-body');
  const submitBtn = document.getElementById('err-preflight-submit-btn');

  if (!modal) return;
  _errPendingSubmit = { sig, entry };
  if (titleEl) titleEl.textContent = 'Submit error upstream';
  if (subEl) subEl.textContent = 'Checking for existing GitHub issues…';
  if (bodyEl) bodyEl.innerHTML = '<div style="text-align:center;padding:20px 0;color:var(--text3)">Checking…</div>';
  if (submitBtn) submitBtn.disabled = true;
  modal.classList.add('open');

  // Pre-flight: search GitHub Issues (unauthenticated, 60 req/hr is fine for deliberate submissions)
  let matchingIssues = [];
  try {
    if (keywords) {
      const ghRepo = await _errFetchRepo();
      const q = encodeURIComponent(`repo:${ghRepo} is:issue is:open ${keywords.replace(/\+/g,' ')}`);
      const ghR = await fetch(`https://api.github.com/search/issues?q=${q}&per_page=5`, {
        headers: { 'Accept': 'application/vnd.github+json' },
      });
      if (ghR.ok) {
        const ghD = await ghR.json();
        matchingIssues = (ghD.items || []).slice(0, 5);
      }
    }
  } catch (_) {}

  // Build issue URL
  const issueTitle = `[error] ${(entry.title || '').slice(0, 120)}`;
  const issueBody = [
    `**Error signature:** \`${entry.fingerprint || entry.signature}\``,
    `**Module:** ${entry.module || '—'}`,
    `**Occurrences:** ${entry.count ?? 1}  (first: ${entry.first_seen || '?'}, last: ${entry.last_seen || '?'})`,
    '',
    '**Sample log line:**',
    '```',
    (entry.sample || '').slice(0, 800),
    '```',
    '',
    '---',
    '*Submitted via Evolve Admin → Errors page*',
  ].join('\n');

  const ghRepo2 = await _errFetchRepo();
  if (!ghRepo2) {
    // No feedback target configured — fail loud instead of opening a
    // malformed github.com//issues/new URL that 404s on the user.
    if (subEl) subEl.textContent = 'Feedback is not configured on this install. Set github_repo in ~/.evolve/feedback-config.json on the deploy box, then retry.';
    _errPendingSubmit.url = '';
    if (submitBtn) submitBtn.disabled = true;
    return;
  }
  const issueUrl = `https://github.com/${ghRepo2}/issues/new?` +
    `title=${encodeURIComponent(issueTitle)}&` +
    `body=${encodeURIComponent(issueBody)}&` +
    `labels=${encodeURIComponent('user-reported,error')}`;

  _errPendingSubmit.url = issueUrl;

  if (matchingIssues.length > 0) {
    if (subEl) subEl.textContent = `${matchingIssues.length} similar issue${matchingIssues.length !== 1 ? 's' : ''} already open on GitHub:`;
    if (bodyEl) {
      bodyEl.innerHTML =
        matchingIssues.map(i =>
          `<a class="gh-issue-link" href="${escHtml(i.html_url)}" target="_blank" rel="noopener">#${i.number} — ${escHtml((i.title||'').slice(0,80))}</a>`
        ).join('') +
        '<div style="font-size:0.78rem;color:var(--text3);margin-top:10px">Your error may already be tracked. Open the issue links above before creating a new one. If it\'s a different problem, click <strong>Create new anyway</strong>.</div>';
      if (submitBtn) submitBtn.textContent = 'Create new anyway →';
    }
  } else {
    if (subEl) subEl.textContent = 'No matching open issues found. Ready to create a new one.';
    if (bodyEl) bodyEl.innerHTML = `<div style="font-size:0.8rem;color:var(--text2)">A pre-filled GitHub issue will open in your browser. Review the content, edit if needed, then post under your GitHub account.</div>`;
    if (submitBtn) submitBtn.textContent = 'Open GitHub Issue →';
  }
  if (submitBtn) submitBtn.disabled = false;
}

let _errCachedRepo = '';
async function _errFetchRepo() {
  if (_errCachedRepo) return _errCachedRepo;
  try {
    const r = await fetch('/api/feedback/config');
    if (r.ok) {
      const d = await r.json();
      _errCachedRepo = d.github_repo || '';
    }
  } catch (_) {}
  return _errCachedRepo;
}

function _errPreflightProceed() {
  const { sig, url } = _errPendingSubmit;
  if (!url) return;
  window.open(url, '_blank', 'noopener');
  // Mark submitted locally
  _errSetStatus(sig, 'submitted');
  _errSetSubmittedUrl(sig, url);
  document.getElementById('err-preflight-modal')?.classList.remove('open');
  _renderErrorsBadge();
  _renderErrorsTable();
  toast('Issue opened in browser', 'ok');
}

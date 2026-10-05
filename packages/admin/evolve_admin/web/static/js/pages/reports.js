// ════════════════════════════════════════════════════════════════════════
// Page: Reports (scheduled metric digests)
//
// V2 three-bucket renderer for the Reports & Alerts page. Hosts:
//   - Schedule config (frequency, time-of-day, weekly day picker)
//   - Per-report subscription rules + snooze handling
//   - Threshold matrix (per-scope override grid, pod default + bot rows)
//   - History viewer (per-report sparkline + traffic-light status)
//
// State + constants:
//   _RA_TRAFFIC, _RA_OVERRIDE_META, _RA_BUCKET_EMOJI, _RA_BUCKET_LABEL,
//   _RA_SNOOZE_KEY, _RA_SNOOZE_HOURS — lookup tables for the three-
//   bucket rendering + snooze persistence.
//   let _raThresholdsMatrix — cached matrix from /api/reports-alerts/thresholds
//
// Loaders dispatched from onPageActivate('reports') + the report-page
// subtab activators:
//   loadReportsAlerts()    — entry point; renders the Recent Reports tab
//   _raLoadSchedule()      — schedule subtab
//   _raLoadStatus()        — current bucket status + sparklines
//   _raLoadSnoozes()       — snooze rule list
//   _raLoadThresholds()    — threshold-override matrix
//   _raLoadHistory()       — history table with expandable bodies
//
// Out of scope (other pages despite "Alerts" naming):
//   loadAlerts (~line 48215) — the standalone Alerts page; separate
//   page despite both surfaces sharing some signal-store concepts.
// ════════════════════════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════
// Reports (scheduled metric digests) — v2 three-bucket renderer
// ══════════════════════════════════════════════════════
const _RA_TRAFFIC = {green:'🟢', yellow:'🟡', red:'🔴'};

// pod_report v2 override keys + human labels. Mirrors DEFAULT_OVERRIDES in
// packages/analyzer/pod_report.py — keep in sync if that dict changes.
const _RA_OVERRIDE_META = {
  'cost_anomaly_factor':        {label:'Cost spike factor (× of 30d mean)',         hint:'Trigger Trending when a bot\'s daily cost is at least this multiple of its 30-day average.'},
  'cost_anomaly_factor_cold':   {label:'Cost spike factor — cold (<14d data)',      hint:'Stricter factor used when a bot has fewer than 14 days of history.'},
  'cost_min_mean_usd':          {label:'Cost noise floor (USD/day)',                hint:'Skip cost anomaly check for bots whose 30d mean is below this.'},
  'sessions_anomaly_factor':    {label:'Session drop factor (× of 30d mean)',       hint:'Trigger Trending when a bot\'s daily sessions are at most this multiple of its 30-day average.'},
  'sessions_anomaly_factor_cold':{label:'Session drop factor — cold (<14d data)',   hint:'Stricter factor used when a bot has fewer than 14 days of history.'},
  'sessions_min_mean':          {label:'Sessions noise floor (per day)',            hint:'Skip session-drop check for bots whose 30d mean is below this.'},
  'pod_silent_session_floor':   {label:'Pod-silent floor (total sessions)',         hint:'Trigger Broken "pod silent" when total sessions across all bots ≤ this on the reference date.'},
};

async function loadReportsAlerts() {
  // Default subtab is "Recent Reports" — load status + history.
  await Promise.all([_raLoadStatus(), _raLoadHistory()]);
}

async function _raLoadSchedule() {
  const el = document.getElementById('ra-schedule-body');
  if (!el) return;
  try {
    const d = await api('GET', '/api/reports-alerts/config');
    const cfg = d.pod_report || {};
    const hourOpts = (sel) => Array.from({length:24},(_,h)=>`<option value="${h}"${sel==h?' selected':''}>${String(h).padStart(2,'0')}:00</option>`).join('');
    const dayOpts  = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map((d,i)=>`<option value="${i}"${cfg.weekly_day==i?' selected':''}>${d}</option>`).join('');
    el.innerHTML = `
      <div class="form-row" style="margin-bottom:16px">
        <div class="form-field">
          <label>Master switch</label>
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin-top:2px">
            <input type="checkbox" id="ra-enabled" ${cfg.enabled?'checked':''} onchange="_raSaveSchedule()">
            <span>Enabled</span>
          </label>
        </div>
        <div class="form-field">
          <label>Frequency</label>
          <select id="ra-frequency" class="input-w-md">
            <option value="daily"    ${cfg.frequency==='daily'   ?'selected':''}>Daily</option>
            <option value="weekdays" ${cfg.frequency==='weekdays'?'selected':''}>Weekdays only</option>
            <option value="weekly"   ${cfg.frequency==='weekly'  ?'selected':''}>Weekly</option>
          </select>
          <div id="ra-weekly-day-wrap" style="${cfg.frequency==='weekly'?'margin-top:6px':'display:none'}">
            <select id="ra-weekly-day" class="input-w-md" onchange="_raSaveSchedule()">${dayOpts}</select>
          </div>
        </div>
      </div>
      <div class="form-row" style="margin-bottom:16px">
        <div class="form-field">
          <label>Report time</label>
          <select id="ra-report-hour" onchange="_raSaveSchedule()" style="width:88px">${hourOpts(cfg.report_hour??8)}</select>
        </div>
        <div class="form-field">
          <label>Notify when</label>
          <select id="ra-notify-on" class="input-w-lg" onchange="_raSaveSchedule()">
            <option value="always"        ${cfg.notify_on==='always'       ?'selected':''}>Always (even if all clear)</option>
            <option value="yellow_or_red" ${cfg.notify_on==='yellow_or_red'?'selected':''}>Trending or Broken items only</option>
            <option value="red_only"      ${cfg.notify_on==='red_only'     ?'selected':''}>Broken items only</option>
          </select>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:10px">
        <button class="btn btn-ghost btn-sm" onclick="_raSendTestReport()">🔔 Send test report now</button>
        <span id="ra-schedule-status" style="font-size:0.78rem"></span>
      </div>
      <pre id="ra-report-preview" style="display:none;margin-top:12px;padding:12px;background:var(--bg3);border:1px solid var(--border);border-radius:6px;font-size:0.78rem;color:var(--text2);white-space:pre-wrap;word-break:break-word;max-height:300px;overflow-y:auto"></pre>`;
    // Use onchange assignment (not addEventListener) so re-running _raLoadSchedule()
    // doesn't accumulate duplicate listeners on the frequency select.
    const freqEl = document.getElementById('ra-frequency');
    if (freqEl) freqEl.onchange = function() {
      const wrap = document.getElementById('ra-weekly-day-wrap');
      if (wrap) wrap.style.display = this.value === 'weekly' ? '' : 'none';
      _raSaveSchedule();
    };
  } catch(e) {
    el.innerHTML = `<div class="empty">Error loading config: ${escHtml(String(e))}</div>`;
  }
}

async function _raSaveSchedule() {
  const cfg = {
    enabled: document.getElementById('ra-enabled')?.checked ?? false,
    report_hour: parseInt(document.getElementById('ra-report-hour')?.value || '8'),
    frequency: document.getElementById('ra-frequency')?.value || 'daily',
    weekly_day: parseInt(document.getElementById('ra-weekly-day')?.value || '1'),
    notify_on: document.getElementById('ra-notify-on')?.value || 'always',
  };
  const status = document.getElementById('ra-schedule-status');
  try {
    await api('PATCH', '/api/reports-alerts/config', {pod_report: cfg});
    if (status) { status.textContent = '✓ Saved'; status.style.color = 'var(--green)'; setTimeout(()=>{ if(status) status.textContent=''; }, 2000); }
  } catch(e) {
    if (status) { status.textContent = 'Error: ' + e; status.style.color = 'var(--red)'; }
  }
}

async function _raSendTestReport() {
  const status = document.getElementById('ra-schedule-status');
  const previewEl = document.getElementById('ra-report-preview');
  if (status) { status.textContent = 'Sending…'; status.style.color = 'var(--text2)'; }
  try {
    const d = await api('POST', '/api/reports-alerts/send-test', {real_send: true});
    const ok = d.sent === true;
    if (status) {
      status.textContent = ok ? `✓ Sent — overall: ${d.overall||'?'}` : ('Error: '+(d.error||'unknown'));
      status.style.color = ok ? 'var(--green)' : 'var(--red)';
      setTimeout(()=>{ if(status) status.textContent=''; }, 4000);
    }
    if (previewEl && d.preview) {
      previewEl.style.display = 'block';
      previewEl.textContent = d.preview;
    }
  } catch(e) {
    if (status) { status.textContent = 'Error: '+e; status.style.color = 'var(--red)'; }
  }
}

const _RA_BUCKET_EMOJI = {broken:'🔴', trending:'🟡', queue:'📋'};
const _RA_BUCKET_LABEL = {broken:'Broken', trending:'Trending', queue:'Queue'};
const _RA_SNOOZE_KEY = 'evolve.report.snooze.v1';
const _RA_SNOOZE_HOURS = 24;

// Snooze state lives in localStorage as { [hash]: until_ts_ms }. The hash is
// `bucket:text` (stable per line). Expired entries are pruned on every read.
function _raLoadSnoozes() {
  try {
    const raw = JSON.parse(localStorage.getItem(_RA_SNOOZE_KEY) || '{}');
    const now = Date.now();
    const live = {};
    for (const [k, v] of Object.entries(raw)) if (typeof v === 'number' && v > now) live[k] = v;
    if (Object.keys(live).length !== Object.keys(raw).length) {
      localStorage.setItem(_RA_SNOOZE_KEY, JSON.stringify(live));
    }
    return live;
  } catch(_) { return {}; }
}
function _raSnoozeHash(line) { return btoa(unescape(encodeURIComponent(`${line.bucket}:${line.text}`))).slice(0, 24); }
function _raSnoozeLine(line) {
  const live = _raLoadSnoozes();
  live[_raSnoozeHash(line)] = Date.now() + _RA_SNOOZE_HOURS * 3600 * 1000;
  localStorage.setItem(_RA_SNOOZE_KEY, JSON.stringify(live));
  _raLoadStatus();
}
function _raUnsnoozeLine(hash) {
  const live = _raLoadSnoozes();
  delete live[hash];
  localStorage.setItem(_RA_SNOOZE_KEY, JSON.stringify(live));
  _raLoadStatus();
}

// Inline SVG sparkline. Renders 30 normalized points with the rightmost
// circled (= the value being judged). No external chart library.
function _raSparklineSvg(values, opts) {
  if (!values || values.length < 2) return '';
  const w = opts?.width ?? 110, h = opts?.height ?? 22, pad = 2;
  const min = Math.min(...values), max = Math.max(...values);
  const range = max - min || 1;
  const xStep = (w - 2 * pad) / (values.length - 1);
  const pts = values.map((v, i) => {
    const x = pad + i * xStep;
    const y = h - pad - ((v - min) / range) * (h - 2 * pad);
    return [x, y];
  });
  const path = pts.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  const last = pts[pts.length - 1];
  return `<svg width="${w}" height="${h}" style="vertical-align:middle">
    <path d="${path}" fill="none" stroke="var(--yellow)" stroke-width="1.2" />
    <circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="2.5" fill="var(--yellow)" />
  </svg>`;
}

async function _raLoadStatus() {
  const el = document.getElementById('ra-status-body');
  if (!el) return;
  try {
    const d = await api('GET', '/api/reports-alerts/status');
    if (d.error) {
      el.innerHTML = `<div class="empty">Error: ${escHtml(d.error)}</div>`;
      return;
    }
    const buckets = d.buckets || {broken:[], trending:[], queue:[]};
    const allLines = [...(buckets.broken||[]), ...(buckets.trending||[]), ...(buckets.queue||[])];

    const snoozes = _raLoadSnoozes();
    const visible = allLines.filter(l => !(_raSnoozeHash(l) in snoozes));
    const snoozed = allLines.filter(l => _raSnoozeHash(l) in snoozes);

    // Empty state: optionally show "Last broken" anchor.
    if (visible.length === 0) {
      const summary = d.empty_summary ? ` · ${escHtml(d.empty_summary)}` : '';
      let lastBroken = '';
      if (d.last_non_green) {
        const ts = String(d.last_non_green.generated_at || '').slice(0, 16).replace('T', ' ');
        const fl = d.last_non_green.first_line || '';
        lastBroken = `<div style="margin-top:6px;padding:8px 14px;color:var(--text3);font-size:0.78rem;border-top:1px solid var(--border)">
          Last broken: ${escHtml(ts)} · ${escHtml(fl)}
        </div>`;
      }
      let snoozedNote = '';
      if (snoozed.length) {
        snoozedNote = `<div style="margin-top:6px;padding:8px 14px;color:var(--text3);font-size:0.78rem">
          ${snoozed.length} snoozed line${snoozed.length>1?'s':''}: <a href="#" onclick="event.preventDefault();_raShowSnoozed()" style="color:var(--text3)">show</a>
        </div>`;
      }
      el.innerHTML = `<div style="padding:14px;color:var(--green);font-size:0.95rem">🟢 All clear${summary}</div>${lastBroken}${snoozedNote}`;
      return;
    }

    const card = (line) => {
      const emoji = _RA_BUCKET_EMOJI[line.bucket] || '•';
      const hash = _raSnoozeHash(line);
      const link = line.deeplink
        ? `<a href="${escHtml(line.deeplink)}" style="color:var(--text3);font-size:0.78rem;text-decoration:none" onclick="event.preventDefault();nav(document.querySelector('[data-page=\\'${line.deeplink.replace(/^\/admin\//,'').replace(/^\//,'')}\\']'))">drill in →</a>`
        : '';
      // Sparklines for trending lines that carry meta.sparklines.
      let sparklines = '';
      if (line.bucket === 'trending' && line.meta?.sparklines?.series) {
        const seriesRows = line.meta.sparklines.series.map(s =>
          `<span style="display:inline-flex;align-items:center;gap:6px;margin-right:14px;color:var(--text3);font-size:0.72rem">
             <span style="font-family:var(--mono,monospace);min-width:42px">${escHtml(botLabel(s.bot_id))}</span>
             ${_raSparklineSvg(s.values)}
           </span>`
        ).join('');
        if (seriesRows) {
          sparklines = `<div style="padding:0 12px 10px 42px">${seriesRows}</div>`;
        }
      }
      return `<div style="border-bottom:1px solid var(--border)">
        <div style="display:flex;align-items:center;gap:10px;padding:10px 12px;font-size:0.9rem">
          <span style="font-size:1.1rem">${emoji}</span>
          <span style="color:var(--text);flex:1">${escHtml(line.text)}</span>
          <button class="btn btn-ghost btn-sm" style="font-size:0.72rem" onclick='_raSnoozeLine(${JSON.stringify({bucket:line.bucket,text:line.text})})'>Snooze 24h</button>
          ${link}
        </div>
        ${sparklines}
      </div>`;
    };

    const refDate = d.ref_date ? `Reference: ${escHtml(d.ref_date)} · ` : '';
    let snoozedFooter = '';
    if (snoozed.length) {
      snoozedFooter = `<div style="margin-top:8px;padding:8px 12px;color:var(--text3);font-size:0.78rem">
        ${snoozed.length} snoozed line${snoozed.length>1?'s':''} hidden · <a href="#" onclick="event.preventDefault();_raShowSnoozed()" style="color:var(--text3)">show</a>
      </div>`;
    }
    el.innerHTML = `<div style="font-size:0.75rem;color:var(--text3);margin-bottom:8px">${refDate}Live view of pod_report</div>
      <div style="border-top:1px solid var(--border)">${visible.map(card).join('')}</div>${snoozedFooter}`;
  } catch(e) {
    el.innerHTML = `<div class="empty">Error: ${escHtml(String(e))}</div>`;
  }
}

function _raShowSnoozed() {
  const snoozes = _raLoadSnoozes();
  const items = Object.entries(snoozes).map(([hash, until]) => {
    const remaining = Math.max(0, Math.round((until - Date.now()) / 3600000));
    return `<li style="margin:4px 0;display:flex;align-items:center;gap:10px">
      <code style="font-size:0.72rem;color:var(--text3);flex:1">${escHtml(hash)}</code>
      <span style="font-size:0.72rem;color:var(--text3)">${remaining}h left</span>
      <button class="btn btn-ghost btn-sm" style="font-size:0.72rem" onclick="_raUnsnoozeLine('${escHtml(hash)}')">Unsnooze</button>
    </li>`;
  }).join('');
  if (typeof modal === 'function') {
    modal({title: 'Snoozed Report Lines', body: `<ul style="list-style:none;padding:0;margin:0">${items}</ul>`});
  } else {
    toast('Snoozed lines: ' + Object.keys(snoozes).length, 'ok');
  }
}

// Cached so the per-row save handlers can read the param spec without
// refetching. Populated by _raLoadThresholds().
let _raThresholdsMatrix = null;

function _raFmtPodDefault(p, val) {
  const decimals = (p.decimals == null ? 2 : p.decimals);
  const n = (Number(val || 0)).toFixed(decimals);
  return `${escHtml(p.unit || '')}${n}`;
}

async function _raLoadThresholds() {
  // Two possible mount points: the new Reports body and a legacy
  // ra-thresholds-body (in case any deeplink still hits it).
  const targets = [
    document.getElementById('reports-thresholds-body'),
    document.getElementById('ra-thresholds-body'),
    document.getElementById('maint-thresholds-body'),
  ].filter(Boolean);
  if (!targets.length) return;
  const setAll = html => targets.forEach(t => { t.innerHTML = html; });
  try {
    const d = await api('GET', '/api/reports-alerts/thresholds');
    _raThresholdsMatrix = d.matrix || null;
    if (!_raThresholdsMatrix) {
      setAll('<div class="empty">Threshold matrix unavailable.</div>');
      return;
    }
    setAll(_raRenderThresholdsMatrix(_raThresholdsMatrix));
  } catch (e) {
    setAll(`<div class="empty">Error: ${escHtml(String(e))}</div>`);
  }
}

function _raRenderThresholdsMatrix(m) {
  const params = m.params || [];
  const pod = m.pod || {};
  const bots = m.bots || {};
  const scope = m.scope || {};
  const botIds = Object.keys(bots).sort();

  // Param header — label + unit/default hint, with help in a tooltip so
  // narrow columns don't break mid-word.
  const headerCells = params.map(p => {
    const helpIcon = p.help
      ? `<span title="${escHtml(p.help)}" style="display:inline-block;width:14px;height:14px;line-height:14px;border-radius:50%;border:1px solid var(--text3);color:var(--text3);font-size:0.62rem;text-align:center;margin-left:4px;cursor:help">?</span>`
      : '';
    const scopeNote = scope[p.id] === 'pod'
      ? `<div style="font-size:0.66rem;color:var(--text3);font-weight:400;margin-top:2px">pod-only knob</div>` : '';
    return `<th style="text-align:right;padding:8px 12px;color:var(--text2);font-size:0.78rem;font-weight:500;min-width:140px">
      <div style="display:flex;align-items:center;justify-content:flex-end;gap:2px">${escHtml(p.label)}${helpIcon}</div>
      <div style="font-size:0.68rem;color:var(--text3);font-weight:400;margin-top:2px">Hardcoded: ${_raFmtPodDefault(p, p.default)}</div>
      ${scopeNote}
    </th>`;
  }).join('');

  // Pod-default row. Writes back to network.pod_report.thresholds.
  // Blank cell = no override (falls through to hardcoded).
  const podCells = params.map(p => {
    const cur = pod[p.id];
    const isOverride = (cur !== p.default) && cur != null;
    const overrideTag = isOverride
      ? `<span class="subtle" style="font-size:0.68rem;color:var(--yellow);margin-top:2px">override</span>` : '';
    return `<td style="padding:8px 12px;text-align:right;white-space:nowrap">
      <div style="display:inline-flex;flex-direction:column;align-items:flex-end;gap:0">
        <input type="number" min="${p.min}" step="${p.step}"
               value="${isOverride ? String(cur) : ''}"
               placeholder="${escHtml(_raFmtPodDefault(p, p.default))}"
               data-scope="pod" data-key="${escHtml(p.id)}"
               style="width:104px;text-align:right;padding:5px 8px;background:var(--bg3);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:0.82rem">
        ${overrideTag}
      </div>
    </td>`;
  }).join('');

  const podRow = `<tr style="border-bottom:2px solid var(--border);background:rgba(124,92,255,0.04)">
    <td style="padding:10px 12px;font-weight:600;color:var(--text);font-size:0.82rem">Pod default</td>
    ${podCells}
    <td style="padding:10px 12px;text-align:right">
      <button class="btn btn-primary btn-sm" onclick="_raSaveScopeRow('pod')">Save</button>
      <span id="ra-thr-status-pod" class="subtle" style="font-size:0.72rem;margin-left:8px"></span>
    </td>
  </tr>`;

  // Per-bot rows. Pod-only knobs are read-only on bot rows (gray placeholder).
  const botRows = botIds.map(botId => {
    const row = bots[botId] || {};
    const cells = params.map(p => {
      const cell = row[p.id] || {};
      const v = cell.value;
      const eff = cell.effective;
      const isOverride = cell.source === 'override';
      const isPodOnly = scope[p.id] === 'pod';
      if (isPodOnly) {
        return `<td style="padding:8px 12px;text-align:right;color:var(--text3);font-size:0.78rem">${_raFmtPodDefault(p, eff)}<div style="font-size:0.66rem">(pod-only)</div></td>`;
      }
      const overrideTag = isOverride
        ? `<span class="subtle" style="font-size:0.68rem;color:var(--yellow);margin-top:2px">override</span>` : '';
      return `<td style="padding:8px 12px;text-align:right;white-space:nowrap">
        <div style="display:inline-flex;flex-direction:column;align-items:flex-end;gap:0">
          <input type="number" min="${p.min}" step="${p.step}"
                 value="${v == null ? '' : String(v)}"
                 placeholder="${escHtml(_raFmtPodDefault(p, pod[p.id]))}"
                 data-scope="bot" data-bot="${escHtml(botId)}" data-key="${escHtml(p.id)}"
                 style="width:104px;text-align:right;padding:5px 8px;background:var(--bg3);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:0.82rem">
          ${overrideTag}
        </div>
      </td>`;
    }).join('');
    return `<tr style="border-bottom:1px solid var(--border)">
      <td style="padding:8px 12px;font-family:var(--font-mono);font-size:0.82rem">${escHtml(botLabel(botId))}</td>
      ${cells}
      <td style="padding:8px 12px;text-align:right">
        <button class="btn btn-ghost btn-sm" onclick="_raSaveScopeRow('bot','${escHtml(botId)}')">Save</button>
        <span id="ra-thr-status-${escHtml(botId)}" class="subtle" style="font-size:0.72rem;margin-left:8px"></span>
      </td>
    </tr>`;
  }).join('');

  return `<div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse">
      <thead><tr style="border-bottom:1px solid var(--border)">
        <th style="text-align:left;padding:8px 12px;color:var(--text2);font-size:0.78rem;font-weight:500">Scope</th>
        ${headerCells}
        <th></th>
      </tr></thead>
      <tbody>${podRow}${botRows}</tbody>
    </table>
  </div>`;
}

// Save handler for a single row (pod or bot). Picks up the inputs scoped to
// that row, sends null for empty cells (= reset that key in the override
// map), sends parsed numbers otherwise.
async function _raSaveScopeRow(scope, botId) {
  const sel = scope === 'pod'
    ? '[data-scope="pod"]'
    : `[data-scope="bot"][data-bot="${botId}"]`;
  const inputs = document.querySelectorAll(`#reports-thresholds-body ${sel}`);
  const thresholds = {};
  let errored = false;
  inputs.forEach(inp => {
    if (errored) return;
    const key = inp.dataset.key;
    const raw = (inp.value || '').trim();
    if (raw === '') { thresholds[key] = null; return; }
    const n = parseFloat(raw);
    const minAttr = parseFloat(inp.min);
    const minimum = isFinite(minAttr) ? minAttr : 0;
    if (!isFinite(n) || n < minimum) { errored = true; return; }
    thresholds[key] = n;
  });
  const statusId = scope === 'pod' ? 'ra-thr-status-pod' : `ra-thr-status-${botId}`;
  const status = document.getElementById(statusId);
  if (errored) {
    if (status) { status.style.color = '#ff8c42'; status.textContent = 'Invalid value(s).'; }
    return;
  }
  if (status) { status.style.color = 'var(--muted)'; status.textContent = 'Saving…'; }
  try {
    const body = scope === 'pod' ? {thresholds} : {bot_id: botId, thresholds};
    await api('PATCH', '/api/reports-alerts/thresholds', body);
    if (status) { status.style.color = '#7fff9e'; status.textContent = 'Saved.'; }
    _raLoadThresholds();
  } catch(e) {
    if (status) { status.style.color = '#ff8c42'; status.textContent = 'Error: ' + e; }
  }
}

// Legacy global save — kept for any leftover button refs; routes through the
// new pod-row save so old DOM still works.
async function _raSaveThresholds() { return _raSaveScopeRow('pod'); }

async function _raLoadHistory() {
  const el = document.getElementById('ra-history-body');
  if (!el) return;
  try {
    const d = await api('GET', '/api/reports-alerts/history');
    const entries = d.entries || [];
    if (!entries.length) {
      el.innerHTML = '<div class="empty">No reports yet — once the morning/evening cron fires (or you click "Send test report" on the Schedule tab) entries will appear here.</div>';
      return;
    }
    el.innerHTML = entries.map(e => {
      const dot = _RA_TRAFFIC[e.status] || '⬜';
      const sentTag = e.sent
        ? '<span style="color:var(--green);font-size:0.72rem">sent</span>'
        : '<span style="color:var(--text3);font-size:0.72rem">filtered (notify_on)</span>';
      const summary = `<summary style="display:flex;align-items:center;gap:10px;padding:7px 0;cursor:pointer;list-style:none">
        <span style="font-size:1.1rem">${dot}</span>
        <span style="color:var(--text2);font-size:0.8rem;min-width:140px">${escHtml(e.ts)}</span>
        <span style="color:var(--text);font-size:0.82rem;min-width:80px">${escHtml(e.label)}</span>
        ${sentTag}
        ${e.report_text ? '<span style="color:var(--text3);font-size:0.72rem;margin-left:auto;display:inline-flex;align-items:center;gap:5px"><span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>click to expand</span>' : '<span style="color:var(--text3);font-size:0.72rem;margin-left:auto">(body not stored)</span>'}
      </summary>`;
      const body = e.report_text
        ? `<pre style="margin:4px 0 10px 30px;padding:10px 12px;background:var(--bg3);border:1px solid var(--border);border-radius:4px;font-size:0.78rem;color:var(--text2);white-space:pre-wrap;word-break:break-word;max-height:400px;overflow-y:auto">${escHtml(e.report_text)}</pre>`
        : '';
      return `<details style="border-bottom:1px solid var(--border)">${summary}${body}</details>`;
    }).join('');
  } catch(e) {
    el.innerHTML = `<div class="empty">Error: ${escHtml(String(e))}</div>`;
  }
}

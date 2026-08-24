// ════════════════════════════════════════════════════════════════════════
// Page subtab: Model Catalog (Settings) + API Error Parser (shared)
//
// Two contiguous sub-clusters extracted together:
//
//   Model Catalog (Settings → Models catalog subtab):
//     loadModelCatalog, openModelCatalogModal, closeModelCatalogModal,
//     submitModelCatalog, removeModelFromCatalog
//
//   API Error Parser (shared by Maintenance and Plugins/Keys pages):
//     Lookup table: _PROVIDER_DISPLAY (anthropic / openai / google / xai /
//                   telegram / network)
//     Helpers: _tsAfter, parseApiErrors, renderErrorLabels,
//              _conditionMeta, buildErrNote
//
// State note:
//   _mcEditingModel — the editing-state global. Its `let _mcEditingModel`
//   declaration lives in pages/onboard-modal.js (Phase 3ae split it
//   one line short of where it should have ended); the declaration is
//   still in script-scope so the functions here see it via runtime
//   free-variable lookup.
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - api(), toast(), escHtml(), botLabel() — core/
//   - _ikBot — pages/plugins.js (Phase 3z; bot selector global)
//   - The API Error Parser is read by Maintenance Gateway (Phase 3w) +
//     Plugins page renderers (Phase 3z) via global lookup
// ════════════════════════════════════════════════════════════════════════

async function loadModelCatalog() {
  const el = document.getElementById('model-catalog-table');
  const auditEl = document.getElementById('model-catalog-audit');
  if (!el) return;
  if (!_ikBot) { el.innerHTML = '<div class="empty" style="color:var(--text3)">No bot selected.</div>'; return; }
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  const d = await api('GET', `/api/admin/models/${_ikBot}`);
  if (d.error) {
    el.innerHTML = `<div style="padding:12px;color:var(--danger)">${escHtml(d.error)}</div>`;
    return;
  }
  const catalog = Array.isArray(d) ? d : (d.catalog || d.models || []);
  if (!catalog.length) {
    el.innerHTML = '<div class="empty">No models in catalog. Use <strong>+ Add Model</strong> to add one.</div>';
    return;
  }
  const tierColor = { tier0:'var(--purple)', tier1:'var(--text3)', tier2:'var(--blue)', tier3:'var(--teal)', tier4:'var(--yellow)', tier5:'var(--green)' };
  el.innerHTML = `<div class="resp-table-wrap"><table class="resp-table data-table"><thead><tr>
    <th>Model ID</th><th>Tier</th><th>Account / Routing</th><th>Status</th><th>Last Used</th><th style="text-align:right">Actions</th>
  </tr></thead><tbody>` + catalog.map(m => {
    const mid = m.id || m.model_id || m.name || '—';
    const tier = m.tier || '—';
    const account = m.account || m.routing || '—';
    const enabled = m.enabled !== false;
    const lastUsed = m.last_used ? escHtml(m.last_used) : '<span style="color:var(--text3)">—</span>';
    const tc = tierColor[tier] || 'var(--text2)';
    return `<tr>
      <td><code style="font-size:0.8rem">${escHtml(mid)}</code></td>
      <td><span style="color:${tc};font-weight:600">${escHtml(tier)}</span></td>
      <td style="font-size:0.8rem;color:var(--text2)">${escHtml(account)}</td>
      <td>${enabled ? '<span style="color:var(--green)">✓ enabled</span>' : '<span style="color:var(--text3)">disabled</span>'}</td>
      <td style="font-size:0.78rem">${lastUsed}</td>
      <td data-label="" style="text-align:right;white-space:nowrap">
        <button class="btn btn-ghost btn-sm" style="margin-right:4px"
          onclick="openModelCatalogModal(${escHtml(JSON.stringify(m))})">✎</button>
        <button class="btn btn-ghost btn-sm" style="color:var(--danger)"
          onclick="removeModelFromCatalog(${escHtml(JSON.stringify(mid))})">✕</button>
      </td>
    </tr>`;
  }).join('') + '</tbody></table></div>';
  _respTableLabelize(el);
  // Audit log last 5 edits
  if (d.audit && d.audit.length) {
    auditEl.innerHTML = 'Recent: ' + d.audit.slice(-5).map(a =>
      `<span style="margin-right:8px">${escHtml(a.action||'edit')} ${escHtml(a.model||'')} ${escHtml(a.at||'')}</span>`
    ).join('');
  } else {
    auditEl.innerHTML = '';
  }
}

function openModelCatalogModal(model) {
  _mcEditingModel = model ? (model.id || model.model_id || model.name || null) : null;
  document.getElementById('model-catalog-modal-title').textContent = model ? 'Edit Model' : 'Add Model';
  document.getElementById('mc-model-id').value = model ? (_mcEditingModel || '') : '';
  document.getElementById('mc-model-id').disabled = !!model; // can't rename existing
  document.getElementById('mc-tier').value = model ? (model.tier || 'tier2') : 'tier2';
  document.getElementById('mc-account').value = model ? (model.account || model.routing || '') : '';
  document.getElementById('mc-enabled').checked = model ? model.enabled !== false : true;
  document.getElementById('mc-result').innerHTML = '';
  document.getElementById('model-catalog-modal').classList.add('open');
}

function closeModelCatalogModal() {
  document.getElementById('model-catalog-modal').classList.remove('open');
}

async function submitModelCatalog() {
  const resultEl = document.getElementById('mc-result');
  const modelId = document.getElementById('mc-model-id').value.trim();
  const tier = document.getElementById('mc-tier').value;
  const account = document.getElementById('mc-account').value.trim();
  const enabled = document.getElementById('mc-enabled').checked;
  if (!modelId) { resultEl.innerHTML = '<div class="alert alert-error">Model ID required.</div>'; return; }
  if (!_ikBot) { resultEl.innerHTML = '<div class="alert alert-error">No bot selected.</div>'; return; }
  resultEl.innerHTML = '<div class="loading"><span class="spinner"></span> Saving…</div>';
  const r = await api('PUT', `/api/admin/models/${_ikBot}`, {
    confirm: true,
    catalog: [modelId],
    model: { id: modelId, tier, account, enabled },
    action: _mcEditingModel ? 'update' : 'add',
  });
  if (r.error) {
    resultEl.innerHTML = `<div class="alert alert-error">${escHtml(r.error)}</div>`;
  } else {
    resultEl.innerHTML = '<div class="alert alert-ok">Saved.</div>';
    setTimeout(closeModelCatalogModal, 1200);
    loadModelCatalog();
  }
}

async function removeModelFromCatalog(modelId) {
  if (!await confirmModal({body: `Remove model "${modelId}" from ${_ikBot}'s catalog?`, danger: true})) return;
  const r = await api('PUT', `/api/admin/models/${_ikBot}`, {
    confirm: true, catalog: [], action: 'remove', model: { id: modelId },
  });
  if (r.error) { toast('Failed: ' + r.error, 'error'); return; }
  toast(`Model removed from catalog`, 'ok');
  loadModelCatalog();
}

// ══════════════════════════════════════════════════════
// API Error Parser — shared by Maintenance and Keys pages
// ══════════════════════════════════════════════════════

const _PROVIDER_DISPLAY = {
  anthropic: 'Anthropic', openai: 'OpenAI', google: 'Google',
  xai: 'xAI', moonshot: 'Moonshot', telegram: 'Telegram', network: 'Network',
};

// Ordered: most-specific patterns first
const _ERR_PROVIDER_PATTERNS = [
  // Explicit candidate= (model fallback lines)
  { re: /candidate=([a-z0-9_/-]+)/i,     extract: m => m[1].split('/')[0] },
  // Explicit provider= field
  { re: /provider=([a-z0-9_/-]+)/i,      extract: m => m[1].split('/')[0] },
  // Telegram IP ranges
  { re: /ENETUNREACH\s+(?:149\.154\.|91\.108\.|5\.28\.)/i, extract: () => 'telegram' },
  // Generic unreachable
  { re: /ENETUNREACH/i,                  extract: () => 'network' },
];

const _ERR_CONDITION_PATTERNS = [
  { re: /context overflow/i,
    condition: 'context_overflow', label: 'context overflow', color: 'var(--orange)', icon: '⚠️' },
  { re: /rate_limit|exceeded your current quota|reached your specified API usage limits/i,
    condition: 'quota_exceeded',   label: 'quota exceeded',   color: 'var(--orange)', icon: '🚫' },
  { re: /ENETUNREACH/i,
    condition: 'unreachable',      label: 'unreachable',      color: 'var(--red)',    icon: '🔴' },
  { re: /pricing bootstrap failed/i,
    condition: 'pricing_fail',     label: 'pricing unavailable', color: 'var(--text3)', icon: '⚠️' },
  { re: /LLM request timed out|FailoverError.*timed out/i,
    condition: 'timeout',          label: 'timed out',        color: 'var(--orange)', icon: '⏱' },
  { re: /invalid.api.key|unauthorized|auth.*fail/i,
    condition: 'auth_failed',      label: 'auth failed',      color: 'var(--red)',    icon: '🔑' },
  { re: /candidate_failed/i,
    condition: 'model_failed',     label: 'model failed',     color: 'var(--orange)', icon: '⚡' },
];

// Age threshold for muting "stale" error chips. Anything older than this
// fades and is treated as probably-resolved. Tuned to one hour: long enough
// that a transient 429 from the last heal cycle still shows vibrantly,
// short enough that yesterday's quota_exceeded does not look current.
const _ERR_STALE_AFTER_MS = 60 * 60 * 1000;

/** Compare ISO timestamps as instants (handles mixed offsets correctly). */
function _tsAfter(a, b) {
  if (!a || !b) return false;
  const ta = Date.parse(a), tb = Date.parse(b);
  if (isNaN(ta) || isNaN(tb)) return false;
  return ta > tb;
}

/**
 * Parse raw gateway error log lines into structured alert objects.
 * Returns deduplicated [{provider, condition, label, color, icon, ts, state, recovered_ts?}].
 *
 * `timestamps` is an optional parallel array of ISO strings (or nulls) sourced
 * from heal.py's `recent_errors_ts`. When supplied, each alert's `ts` is the
 * latest timestamp seen across the lines that matched that (provider, condition).
 *
 * `providers` is heal.py's `providers` map ({provider: {last_success_ts}}). When
 * supplied and an alert's provider has a success timestamp later than its error
 * timestamp, the alert is tagged `state: 'recovered'` so the chip renders as
 * `✓ recovered` instead of red. Without it, alerts default to `state: 'active'`.
 */
function parseApiErrors(lines, timestamps, providers) {
  const byKey = new Map();
  const order = [];
  const ts_arr = timestamps || [];
  for (let i = 0; i < (lines || []).length; i++) {
    const line = lines[i];
    const ts = ts_arr[i] || null;
    let provider = null;
    for (const { re, extract } of _ERR_PROVIDER_PATTERNS) {
      const m = line.match(re);
      if (m) { provider = extract(m); break; }
    }
    for (const { re, condition, label, color, icon } of _ERR_CONDITION_PATTERNS) {
      if (re.test(line)) {
        const key = `${provider||''}:${condition}`;
        const existing = byKey.get(key);
        if (!existing) {
          byKey.set(key, { provider, condition, label, color, icon, ts });
          order.push(key);
        } else if (ts && _tsAfter(ts, existing.ts)) {
          existing.ts = ts;
        }
        break;
      }
    }
  }
  const provs = providers || {};
  return order.map(k => {
    const a = byKey.get(k);
    const lastSuccess = (provs[a.provider] || {}).last_success_ts;
    if (lastSuccess && _tsAfter(lastSuccess, a.ts)) {
      a.state = 'recovered';
      a.recovered_ts = lastSuccess;
    } else {
      a.state = 'active';
    }
    return a;
  });
}

/**
 * Render compact colored label chips from parseApiErrors output.
 *
 * Three visual states:
 *   - active fresh:    full color, "· Xm ago" suffix
 *   - active stale:    same chip at 55% opacity (older than _ERR_STALE_AFTER_MS)
 *   - recovered:       green outline + ✓, "· recovered Xh ago" suffix
 *                      (recovery freshness, not error age — the question users
 *                      are asking is "is this fixed now," not "when did it break")
 */
function renderErrorLabels(alerts) {
  const now = Date.now();
  return alerts.map(a => {
    const provStr = a.provider ? `<strong>${escHtml(_PROVIDER_DISPLAY[a.provider] || a.provider)}</strong>: ` : '';
    if (a.state === 'recovered') {
      const ageStr = a.recovered_ts ? ` · recovered ${ago(a.recovered_ts)}` : ' · recovered';
      const titleSuffix = a.recovered_ts && a.ts
        ? ` (provider succeeded at ${escHtml(a.recovered_ts)}, after the ${escHtml(a.ts)} error)`
        : '';
      return `<span style="display:inline-flex;align-items:center;gap:3px;font-size:0.72rem;padding:2px 8px;border-radius:10px;background:rgba(46,160,67,0.10);border:1px solid var(--green);color:var(--green);white-space:nowrap;margin:1px 2px 1px 0" title="${escHtml(a.condition)}${titleSuffix}">✓ ${provStr}${escHtml(a.label)}${ageStr}</span>`;
    }
    let ageMs = null;
    if (a.ts) {
      const t = Date.parse(a.ts);
      if (!isNaN(t)) ageMs = now - t;
    }
    const isStale = ageMs !== null && ageMs > _ERR_STALE_AFTER_MS;
    const ageStr = a.ts ? ` · ${ago(a.ts)}` : '';
    const opacity = isStale ? '0.55' : '1';
    const titleSuffix = a.ts ? ` (last seen ${escHtml(a.ts)}${isStale ? ' — likely resolved' : ''})` : '';
    return `<span style="display:inline-flex;align-items:center;gap:3px;font-size:0.72rem;padding:2px 8px;border-radius:10px;background:rgba(0,0,0,0.15);border:1px solid ${a.color};color:${a.color};white-space:nowrap;margin:1px 2px 1px 0;opacity:${opacity}" title="${escHtml(a.condition)}${titleSuffix}">${a.icon} ${provStr}${escHtml(a.label)}${ageStr}</span>`;
  }).join('');
}

/** Look up label/color/icon for a heal.py-emitted condition string. */
function _conditionMeta(condition) {
  const found = _ERR_CONDITION_PATTERNS.find(p => p.condition === condition);
  return found || { label: condition, color: 'var(--text2)', icon: '⚠️' };
}

/**
 * Build the Recent Errors cell content for a bot status row.
 *
 * Renders three chip categories:
 *   1. Active alerts parsed from `recent_errors` (red, with age + stale fade).
 *   2. Active alerts that the providers-map already shows as recovered (green ✓).
 *   3. Sticky recovered chips from heal.py's `recovered_alerts` whose log lines
 *      have already aged out of `recent_errors` (green ✓; persists for 24h).
 *
 * The sticky third category is what closes the wild-goose-chase gap — a 429 that
 * was hit and recovered six hours ago shows as "✓ recovered" until heal.py
 * decays it, instead of disappearing the moment the log line scrolls off.
 */
function buildErrNote(s, fresh) {
  if (fresh) return `<span style="color:var(--text3);font-size:0.75rem">Not yet checked — heal.py runs every 5 min</span>`;

  const ts = s.recent_errors_ts || [];
  const providers = s.providers || {};
  const active = parseApiErrors(s.recent_errors || [], ts, providers);

  // Sticky recovered chips for (provider, condition) keys not in `active`.
  const activeKeys = new Set(active.map(a => `${a.provider||''}:${a.condition}`));
  const sticky = (s.recovered_alerts || [])
    .filter(r => !activeKeys.has(`${r.provider||''}:${r.condition}`))
    .map(r => {
      const meta = _conditionMeta(r.condition);
      return {
        provider: r.provider, condition: r.condition,
        label: meta.label, color: meta.color, icon: meta.icon,
        ts: r.error_ts, recovered_ts: r.recovered_ts,
        state: 'recovered',
      };
    });

  const allChips = [...active, ...sticky];
  if (!allChips.length && !(s.recent_errors || []).length) {
    return `<span style="color:var(--text3);font-size:0.75rem">none</span>`;
  }

  const labels = renderErrorLabels(allChips);
  const recentCount = (s.recent_errors || []).length;
  const fallback = (allChips.length || !recentCount) ? '' : `<span style="color:var(--red);font-size:0.73rem">▼ ${recentCount} error(s)</span>`;

  // Per-line age prefix in the raw expansion. Uncategorized lines are the only
  // place where a bare "5h ago" is the user's only signal, so it's important.
  const rawLines = (s.recent_errors || []).map((e, i) => {
    const t = ts[i];
    const age = t ? `[${ago(t)}] ` : '';
    return escHtml(age) + escHtml(e);
  }).join('\n');
  const rawDetails = recentCount
    ? `<details style="margin-top:4px;font-size:0.72rem"><summary style="cursor:pointer;color:var(--text3);list-style:none;display:inline-flex;align-items:center;gap:5px"><span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>${recentCount} raw line(s)</summary><pre style="margin-top:6px;white-space:pre-wrap;word-break:break-all;color:var(--text2);max-height:120px;overflow-y:auto;font-size:0.7rem">${rawLines}</pre></details>`
    : '';
  return `<div style="line-height:1.8">${labels}${fallback}${rawDetails}</div>`;
}


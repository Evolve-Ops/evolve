// ════════════════════════════════════════════════════════════════════════
// Evolve overhead — the Cost page's "what does Evolve itself cost" panel.
//
// Brief evolve-overhead-ledger-and-budget (D-OH5, internal/decision-evolve-
// overhead-2026-09-07.md). One row per bot for the rolling 7 days, expandable
// by Evolve call kind (and, under it, by call site). The number is Evolve's
// own model calls plus an ESTIMATE of the injected-context cost, as a share of
// the bot's total spend, against the target in network.json evolve.overhead.
//
// Data: GET /api/analytics/evolve-overhead (a thin re-shape of the analyzer's
// ledger — nothing is re-derived here). A tripped bot gets a card: Evolve's
// own machinery is paused, the bot is still answering, and the card names the
// caller in words. "Resume" is Evolve's — it does not touch the bot's cost
// breaker.
//
// Loaded by the SPA's pages/ scan; called from cost.js loadUsageCost().
// ════════════════════════════════════════════════════════════════════════

const _EO_KIND_LABELS = {
  classifier: 'Classifiers & routing',
  summarizer: 'Session summaries',
  task_extractor: 'Task extraction',
};
const _EO_TAG_LABELS = {
  'preflight': "Evolve's own routing calls",
  'tier-classifier': 'Tier classification',
  'session-judge': 'Session judging',
  'session-summary': 'Session summaries',
  'unknown': 'call site not recorded',
};

function _eoUsd(n) { return n != null ? '$' + Number(n).toFixed(2) : '—'; }
function _eoPct(n) { return n != null ? (Number(n) * 100).toFixed(1) + '%' : 'n/a'; }

function _eoSourceBadge(src) {
  if (!src) return '';
  const label = src === 'oc' ? 'billed' : src;
  const title = src === 'oc'
    ? "Priced from OpenClaw's own per-call cost"
    : src === 'mixed' ? "Some calls priced by OpenClaw, some by Evolve's price table"
    : "Evolve's price table — OpenClaw did not price these calls (an estimate)";
  return `<span class="badge" title="${escHtml(title)}">${escHtml(label)}</span>`;
}

function _eoCardHtml(bot, card) {
  if (!card || !card.tripped) return '';
  const caller = card.top_session_key
    ? `<div style="margin-top:2px;color:var(--text3)">Top caller: ${escHtml(card.top_prefix_words || '')} · <code>${escHtml(card.top_session_key)}</code> · ${Number(card.top_count || 0).toLocaleString()} calls</div>`
    : '';
  const paused = (card.paused || []).map(p => `<li>${escHtml(p)}</li>`).join('');
  return `<div class="card" style="margin-bottom:8px;border-color:var(--yellow)">
    <div style="font-weight:600;color:var(--yellow)">${escHtml(bot)} — ${escHtml(card.headline || '')}</div>
    <div style="font-size:0.78rem;color:var(--text2);margin-top:4px">${escHtml(card.reason || '')}</div>
    ${caller}
    <div style="font-size:0.75rem;color:var(--text3);margin-top:4px">Paused: <ul style="margin:2px 0 0 18px">${paused}</ul></div>
    <div style="margin-top:6px"><button class="btn btn-ghost btn-sm" onclick="resumeEvolveOverhead('${escHtml(bot)}')" title="${escHtml(card.resume_hint || '')}">Resume Evolve's machinery</button>
      <span style="font-size:0.72rem;color:var(--text3);margin-left:8px">Counts from the moment you resume; it does not re-judge the calls that tripped this.</span></div>
  </div>`;
}

function _eoRows(bot, idx, w, isPod) {
  const kinds = Object.entries((w && w.by_kind) || {})
    .sort((a, b) => (b[1].usd || 0) - (a[1].usd || 0));
  const hasKinds = kinds.length > 0 && !isPod;
  const over = w.over_target;
  const shareCell = `<span style="color:${over ? 'var(--orange)' : 'inherit'}">${_eoPct(w.share)}${over ? ' ⚠' : ''}</span>`;
  const head = `<tr class="eo-row" data-eo="${idx}">
    <td data-label="Bot" style="font-size:0.78rem">${hasKinds
      ? `<span class="expand-icon" aria-hidden="true" onclick="toggleEvolveOverheadRow(${idx})" style="cursor:pointer"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span> `
      : ''}${isPod ? '<strong>Pod</strong>' : escHtml(bot)}</td>
    <td data-label="Total spend" style="font-size:0.78rem">${_eoUsd(w.total_usd)}</td>
    <td data-label="Evolve calls" style="font-size:0.78rem">${Number(w.evolve_calls || 0).toLocaleString()} ${_eoSourceBadge(w.evolve_cost_source)}</td>
    <td data-label="Calls $" style="font-size:0.78rem">${_eoUsd(w.evolve_usd)}</td>
    <td data-label="Context (est.)" style="font-size:0.78rem" title="Estimate: tool-schema tokens × answering turns × the answering model's cached-input price">~${_eoUsd(w.context_usd)}</td>
    <td data-label="Overhead" style="font-size:0.78rem">${_eoUsd(w.overhead_usd)}</td>
    <td data-label="Share" style="font-size:0.78rem">${shareCell}</td>
    <td data-label="Calls / message" style="font-size:0.78rem">${w.calls_per_user_turn != null ? Number(w.calls_per_user_turn).toFixed(2) : 'n/a'}</td>
    <td data-label="A person / no speaker" style="font-size:0.75rem;color:var(--text2)" title="Spend on turns with a resolved human speaker vs spend with none (cron, heartbeat, loops)">${_eoUsd(w.attributed_usd)} / ${_eoUsd(w.unattributed_usd)}</td>
  </tr>`;
  const sub = hasKinds ? kinds.map(([kind, kb]) => {
    const tags = Object.entries(kb.tags || {}).sort((a, b) => (b[1].usd || 0) - (a[1].usd || 0));
    const kindRow = `<tr class="eo-sub" data-eo="${idx}" hidden>
      <td data-label="Kind" style="font-size:0.75rem;padding-left:28px;color:var(--text2)">${escHtml(_EO_KIND_LABELS[kind] || kind)}</td>
      <td></td><td style="font-size:0.75rem;color:var(--text2)">${Number(kb.calls || 0).toLocaleString()}</td>
      <td style="font-size:0.75rem;color:var(--text2)">${_eoUsd(kb.usd)}</td><td colspan="5"></td></tr>`;
    const tagRows = tags.length > 1 || (tags[0] && tags[0][0] !== 'unknown') ? tags.map(([tag, tb]) => `<tr class="eo-sub" data-eo="${idx}" hidden>
      <td data-label="Call site" style="font-size:0.72rem;padding-left:48px;color:var(--text3)">${escHtml(_EO_TAG_LABELS[tag] || tag)}</td>
      <td></td><td style="font-size:0.72rem;color:var(--text3)">${Number(tb.calls || 0).toLocaleString()}</td>
      <td style="font-size:0.72rem;color:var(--text3)">${_eoUsd(tb.usd)}</td><td colspan="5"></td></tr>`).join('') : '';
    return kindRow + tagRows;
  }).join('') : '';
  return head + sub;
}

function toggleEvolveOverheadRow(idx) {
  const root = document.getElementById('evolve-overhead-table');
  if (!root) return;
  const row = root.querySelector(`tr.eo-row[data-eo="${idx}"]`);
  const icon = row ? row.querySelector('.expand-icon') : null;
  const willOpen = icon ? !icon.classList.contains('is-open') : true;
  root.querySelectorAll(`tr.eo-sub[data-eo="${idx}"]`).forEach(tr => { tr.hidden = !willOpen; });
  if (icon) icon.classList.toggle('is-open', willOpen);
}
window.toggleEvolveOverheadRow = toggleEvolveOverheadRow;

async function resumeEvolveOverhead(bot) {
  const r = await api('POST', `/api/evolve-overhead/${encodeURIComponent(bot)}/resume`, {});
  if (r && r.ok) {
    const n = r.accepted && r.accepted.accepted_calls;
    toast(`Evolve resumed for ${bot}${n ? ` — accepted ${n} calls, counting from now` : ''}`, 'ok');
  } else {
    toast((r && r.error) || 'Could not resume Evolve', 'err');
  }
  if (typeof _renderEvolveOverhead === 'function') await _renderEvolveOverhead();
  if (typeof loadStatus === 'function') { await loadStatus(); }
}
window.resumeEvolveOverhead = resumeEvolveOverhead;

async function _renderEvolveOverhead() {
  const table = document.getElementById('evolve-overhead-table');
  const cards = document.getElementById('evolve-overhead-cards');
  const note = document.getElementById('evolve-overhead-note');
  if (!table) return;
  table.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';
  const d = await api('GET', '/api/analytics/evolve-overhead');
  if (!d || d.error) {
    table.innerHTML = `<div class="empty">${escHtml((d && d.error) || 'Failed to load the Evolve overhead ledger.')}</div>`;
    if (cards) cards.innerHTML = '';
    if (note) note.innerHTML = '';
    return;
  }
  const bots = Object.keys(d.bots || {}).sort(
    (a, b) => ((d.bots[b].d7 || {}).share || 0) - ((d.bots[a].d7 || {}).share || 0));
  if (cards) cards.innerHTML = bots.map(b => _eoCardHtml(b, d.bots[b].card)).join('');
  const measured = bots.filter(b => d.bots[b].measured && d.bots[b].d7);
  if (!measured.length) {
    table.innerHTML = '<div class="empty">Overhead not measured yet — no turn files could be read for any bot.</div>';
    if (note) note.innerHTML = '';
    return;
  }
  const head = '<div class="resp-table-wrap"><table class="resp-table resp-table-dense"><thead><tr>'
    + '<th>Bot</th><th>Total spend</th><th title="Evolve\'s own model calls: classifiers, routing, summaries">Evolve calls</th>'
    + '<th>Calls $</th><th title="Estimate — see the note below">Context (est.)</th><th>Overhead</th>'
    + '<th title="Overhead as a share of the bot\'s total spend">Share</th><th title="Evolve model calls per message to the bot">Calls / message</th>'
    + '<th data-secondary title="A person working the bot / spend with no speaker">Person / no speaker</th>'
    + '</tr></thead><tbody>';
  const body = measured.map((b, i) => _eoRows(b, i, d.bots[b].d7, false)).join('');
  const pod = d.pod && d.pod.d7 ? _eoRows('pod', 'pod', d.pod.d7, true) : '';
  table.innerHTML = head + body + `<tr style="border-top:1px solid var(--border)"></tr>` + pod + '</tbody></table></div>';
  if (note) {
    const cfg = d.config || {};
    const unread = (d.unreadable_bots || []).length
      ? ` Could not read turns for: ${d.unreadable_bots.map(escHtml).join(', ')}.` : '';
    note.innerHTML = `<div style="font-size:0.78rem;color:var(--text2)">Target: Evolve's own overhead ≤ <strong>${_eoPct(cfg.share_max)}</strong> of a bot's spend and ≤ <strong>${cfg.calls_per_user_turn_max}</strong> Evolve model call per message. Calls are billed amounts where OpenClaw priced them; <strong>context is an estimate</strong> (tool-schema tokens × turns × the model's cached-input price). Over target for a rolling hour, Evolve pauses its own routing, judges and summaries for that bot — the bot keeps answering.${unread}</div>`;
  }
}
window._renderEvolveOverhead = _renderEvolveOverhead;

// ════════════════════════════════════════════════════════════════════════
// PA week cost — the Cost page's daily row per bot (brief pa-week-cost-is-
// measured). Per bot per day: what OpenClaw billed (cost truth), Evolve's
// overhead share, turns and dollars by the tier that answered, housekeeping
// apart, and the day's largest single turn. The "standard-default" column is
// an ESTIMATE — Power-tier turns repriced on the standard rung.
//
// Also the operator's reading: one optional pool used / ceiling per day, typed
// from the usage page. Never inferred, never carried onto a day without one.
//
// Data: GET /api/analytics/pa-week-cost; POST .../reading. Called from
// cost.js loadUsageCost().
// ════════════════════════════════════════════════════════════════════════

function _pwUsd(n) { return n != null ? '$' + Number(n).toFixed(2) : '—'; }
function _pwPct(n) { return n != null ? (Number(n) * 100).toFixed(1) + '%' : 'n/a'; }

function _pwTierCell(t) {
  return ['fast', 'standard', 'power'].map(k => {
    const b = (t || {})[k] || { turns: 0, usd: 0 };
    return `${k} ${Number(b.turns).toLocaleString()} · ${_pwUsd(b.usd)}`;
  }).join('<br>');
}

function _pwRow(bot, r, reading) {
  const lt = r.largest_turn;
  const unk = ((r.tiers || {}).unknown || {}).turns || 0;
  const oth = ((r.tiers || {}).other || {}).turns || 0;
  const extra = (unk || oth)
    ? `<br><span style="color:var(--text3)">${unk ? unk + ' tier unknown' : ''}${unk && oth ? ' · ' : ''}${oth ? oth + ' other' : ''}</span>` : '';
  const rd = reading
    ? `${_pwUsd(reading.used_usd)}${reading.ceiling_usd != null ? ' / ' + _pwUsd(reading.ceiling_usd) : ''}`
    : '<span style="color:var(--text3)">not recorded</span>';
  return `<tr>
    <td data-label="Bot" style="font-size:0.78rem">${escHtml(bot)}</td>
    <td data-label="Day" style="font-size:0.78rem">${escHtml(r.day)}</td>
    <td data-label="Billed" style="font-size:0.78rem">${_pwUsd(r.total_usd)}${r.unpriced_turns ? ' <span class="badge" title="Some turns could not be priced; this is a floor">floor</span>' : ''}</td>
    <td data-label="Evolve share" style="font-size:0.78rem">${_pwPct((r.overhead || {}).share)}</td>
    <td data-label="By tier" style="font-size:0.75rem">${_pwTierCell(r.tiers)}${extra}</td>
    <td data-label="Housekeeping" style="font-size:0.78rem">${_pwUsd((r.housekeeping || {}).usd)}</td>
    <td data-label="Largest turn" style="font-size:0.78rem">${lt ? _pwUsd(lt.usd) + ' <span style="color:var(--text3)">' + escHtml(lt.tier || '') + '</span>' : '—'}</td>
    <td data-label="Standard default (est.)" style="font-size:0.78rem" title="Estimate: Power-tier turns repriced on the standard rung from their own token counts">~${_pwUsd(r.standard_default_estimate_usd)}</td>
    <td data-label="Pool (you)" style="font-size:0.78rem">${rd}</td>
  </tr>`;
}

async function savePaWeekReading() {
  const val = id => (document.getElementById(id) || {}).value || '';
  const r = await api('POST', '/api/analytics/pa-week-cost/reading', {
    day: val('pw-day'), used_usd: val('pw-used'), ceiling_usd: val('pw-ceiling'), note: val('pw-note'),
  });
  if (r && r.ok) toast('Pool reading saved', 'ok');
  else toast((r && r.error) || 'Could not save the reading', 'err');
  await _renderPaWeekCost();
}
window.savePaWeekReading = savePaWeekReading;

async function _renderPaWeekCost() {
  const table = document.getElementById('pa-week-cost-table');
  if (!table) return;
  const note = document.getElementById('pa-week-cost-note');
  table.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';
  const d = await api('GET', '/api/analytics/pa-week-cost');
  if (!d || d.error) {
    table.innerHTML = `<div class="empty">${escHtml((d && d.error) || 'Failed to load the week.')}</div>`;
    if (note) note.innerHTML = '';
    return;
  }
  const readings = d.operator_readings || {};
  const rows = [];
  Object.keys(d.bots || {}).sort().forEach(bot => {
    (d.bots[bot].days || []).forEach(r => rows.push(_pwRow(bot, r, readings[r.day])));
  });
  if (!rows.length) {
    table.innerHTML = '<div class="empty">No bot turn files could be read.</div>';
  } else {
    table.innerHTML = '<div class="resp-table-wrap"><table class="resp-table resp-table-dense"><thead><tr>'
      + '<th>Bot</th><th>Day</th><th title="OpenClaw\'s own per-call cost, summed">Billed</th><th>Evolve share</th>'
      + '<th>By tier that answered</th><th>Housekeeping</th><th>Largest turn</th>'
      + '<th title="Estimate">Standard default (est.)</th><th title="What you typed from your usage page">Pool used / monthly ceiling</th>'
      + '</tr></thead><tbody>' + rows.join('') + '</tbody></table></div>';
  }
  if (note) {
    const unread = (d.unreadable_bots || []).length
      ? ` Could not read turns for: ${d.unreadable_bots.map(escHtml).join(', ')}.` : '';
    note.innerHTML = `<div style="font-size:0.78rem;color:var(--text2)">${escHtml(d.fit_sentence || '')}${unread}</div>`;
  }
}
window._renderPaWeekCost = _renderPaWeekCost;

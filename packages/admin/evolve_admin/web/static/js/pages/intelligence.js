// ════════════════════════════════════════════════════════════════════════
// Page: Intelligence — the living window into the pod dossier
//
// internal/design-pod-dossier-2026-08-24.md (D-T2 living surface, D-T5
// module cards + interest profile, D-T7 weekly rhythm, D-T8 the plain-
// English bar, D-T9 this page is the asset's home).
// Backend: web/routes_dossier.py · synthesis: packages/analyzer/dossier/.
//
// THE ONE RULE THIS FILE IS BUILT AROUND: **pages don't think.** Every
// operator-facing sentence on this screen was written by the synthesis
// layer, gated by tools/readability-lint, and handed to this file as a
// string. Nothing here composes a sentence, derives a number, decides
// what a week means, or formats a currency. If you find yourself about
// to build a phrase from fields, the phrase belongs in
// dossier/headlines.py (where the gate can read it) or in
// routes_dossier.py (where the tests can score it) — not here.
//
// THE THREE HONESTY RULES ON THE SCREEN:
//
//   * A module with nothing to measure says so, in its own words, and
//     never draws a zero. `measurable:false` cards render the headline
//     and stop; they do not get an empty chart.
//   * A young pod is told it is young — ONCE. Fewer than two weeks on
//     record means no trend line and, in its place on each card, that
//     module's own line about what will stand there next week. The fact
//     that the pod has no history at all is said in the week bar, above
//     the grid, and nowhere else: it used to be appended to all four
//     headlines, which is four copies of one sentence on the first page a
//     new operator ever sees.
//   * THIS WEEK is drawn before history is. The bars and the 28-day strip
//     need no earlier week, so a pod installed this morning still gets
//     pictures; the sparkline joins them the week a second week exists.
//   * NO FILTER BUBBLE (design §4a rule 2). Hiding a module collapses
//     and de-emphasizes it; for a module the house marks `critical` it
//     does not silence it. A critical module the operator hid still
//     renders — muted, with its headline intact and a line saying why it
//     is still here. The operator most likely to hide the security card
//     is the one who most needs to see it.
//
// WHY THE WORD "EDITION" IS NOWHERE ON SCREEN: it is in
// dossier.readability's JARGON set — our word, not the reader's. The
// operator sees "week". The routes and the store keep the internal name.
// ════════════════════════════════════════════════════════════════════════

// Everything the page is currently showing. `weekId` null = the current
// week (whatever /api/dossier/current returned).
const _piState = {
  loaded: false,
  available: false,
  weekId: null,
  week: null,
  weeks: [],
  modules: [],
  // The operator's arrangement. Mirrored locally so a click repaints at
  // once and the write happens behind it; a failed write refetches rather
  // than leaving the screen disagreeing with the disk.
  profile: { order: [], hidden: [], ratings: {} },
};

// ── activation + loading ───────────────────────────────────────────────────

function intelligencePageActivate() {
  _piRender();                       // paint the loading state immediately
  _piLoad(_piState.weekId);
}

async function _piLoad(weekId) {
  const path = weekId
    ? `/api/dossier/editions/${encodeURIComponent(weekId)}`
    : '/api/dossier/current';
  // One round trip each, in parallel: the week and the arrangement are
  // independent reads and the page needs both before it can paint.
  const [week, profile] = await Promise.all([
    api('GET', path),
    api('GET', '/api/dossier/profile'),
  ]);
  if (!week || week.error) {
    _piState.loaded = true;
    _piState.available = false;
    _piState.error = (week && week.error) || 'the request failed';
    _piRender();
    return;
  }
  _piState.error = null;
  _piState.loaded = true;
  _piState.available = !!week.available;
  _piState.week = week.week || null;
  _piState.weeks = week.weeks || [];
  _piState.weekId = week.week ? week.week.id : null;
  _piState.modules = week.modules || [];
  if (profile && profile.profile) _piState.profile = profile.profile;
  _piRender();
}

function _piSelectWeek(weekId) {
  // The current week is fetched through /current so the page always knows
  // which one IS current, rather than inferring it from the list.
  const isCurrent = _piState.weeks.some(w => w.id === weekId && w.is_current);
  _piState.weekId = isCurrent ? null : weekId;
  // `loaded` deliberately stays true: the previous week's cards hold until
  // the new ones arrive. Blanking the grid to a skeleton for one round trip
  // is a layout jump in exchange for nothing (house dataviz rule — hold the
  // previous render on refetch).
  _piLoad(_piState.weekId);
}

// ── the operator's arrangement ─────────────────────────────────────────────

// Modules in the operator's order. Anything the profile has never heard of
// (a module that shipped after the last save) goes to the end in the order
// synthesis wrote it — appended, never dropped: an unranked module is a new
// module, and a new module the operator has never seen must not be invisible.
function _piOrdered() {
  const order = _piState.profile.order || [];
  const byId = new Map(_piState.modules.map(m => [m.module_id, m]));
  const out = [];
  order.forEach(id => { if (byId.has(id)) { out.push(byId.get(id)); byId.delete(id); } });
  _piState.modules.forEach(m => { if (byId.has(m.module_id)) out.push(m); });
  return out;
}

function _piIsHidden(module) {
  return (_piState.profile.hidden || []).indexOf(module.module_id) !== -1;
}

// Design §4a rule 2, in one function. A critical module is never removed
// from the grid — hidden only mutes it.
function _piShowsInGrid(module) {
  return module.critical || !_piIsHidden(module);
}

async function _piSaveProfile() {
  const body = {
    order: _piOrdered().map(m => m.module_id),
    hidden: _piState.profile.hidden || [],
    ratings: _piState.profile.ratings || {},
  };
  const r = await api('POST', '/api/dossier/profile', body);
  if (!r || r.error || !r.ok) {
    toast("Couldn't save how you arranged this page — reloading it.", 'err');
    _piLoad(_piState.weekId);
    return;
  }
  _piState.profile = r.profile;
}

function _piRate(moduleId, verdict) {
  const ratings = Object.assign({}, _piState.profile.ratings || {});
  if (ratings[moduleId] === verdict) delete ratings[moduleId];
  else ratings[moduleId] = verdict;
  _piState.profile.ratings = ratings;
  _piRender();
  _piSaveProfile();
}

function _piHide(moduleId) {
  const hidden = (_piState.profile.hidden || []).slice();
  if (hidden.indexOf(moduleId) === -1) hidden.push(moduleId);
  _piState.profile.hidden = hidden;
  _piState.profile.order = _piOrdered().map(m => m.module_id);
  _piRender();
  _piSaveProfile();
  const module = _piState.modules.find(m => m.module_id === moduleId);
  toast(module && module.critical
    ? 'Turned down, not turned off — this one matters too much to hide.'
    : "Hidden here. It's still in your dossier.");
}

function _piShow(moduleId) {
  _piState.profile.hidden =
    (_piState.profile.hidden || []).filter(id => id !== moduleId);
  _piRender();
  _piSaveProfile();
}

// Move within the VISIBLE run, then write the whole order back (hidden
// modules keep their slot, so un-hiding restores position rather than
// dropping the card at the end).
function _piMove(moduleId, delta) {
  const all = _piOrdered();
  const visible = all.filter(_piShowsInGrid);
  const from = visible.findIndex(m => m.module_id === moduleId);
  const to = from + delta;
  if (from < 0 || to < 0 || to >= visible.length) return;
  const a = all.findIndex(m => m.module_id === moduleId);
  const b = all.findIndex(m => m.module_id === visible[to].module_id);
  const swapped = all.slice();
  swapped[a] = all[b];
  swapped[b] = all[a];
  _piState.profile.order = swapped.map(m => m.module_id);
  _piRender();
  _piSaveProfile();
}

// ── render ─────────────────────────────────────────────────────────────────

function _piRender() {
  const bar = document.getElementById('pi-weekbar');
  const grid = document.getElementById('pi-grid');
  const tray = document.getElementById('pi-tray');
  if (!grid) return;

  if (!_piState.loaded) {
    if (bar) bar.innerHTML = '';
    grid.innerHTML = `<div class="empty-state-card">Loading…</div>`;
    if (tray) tray.innerHTML = '';
    return;
  }
  if (_piState.error) {
    if (bar) bar.innerHTML = '';
    grid.innerHTML = `<div class="empty-state-card">Couldn't load this page — ${escHtml(String(_piState.error))}.
      <div class="pi-sub">Showing nothing rather than something stale. Try refresh; if it keeps failing, check the admin log.</div></div>`;
    if (tray) tray.innerHTML = '';
    return;
  }
  if (!_piState.available) {
    if (bar) bar.innerHTML = '';
    grid.innerHTML = `<div class="empty-state-card">Nothing here yet — Evolve writes this once a week, on Monday morning.
      <div class="pi-sub">A pod that was set up this week gets its first one on the next Monday. Nothing is missing; there just isn't a week to look back on yet.</div></div>`;
    if (tray) tray.innerHTML = '';
    return;
  }

  if (bar) bar.innerHTML = _piWeekBar();
  const visible = _piOrdered().filter(_piShowsInGrid);
  grid.innerHTML = visible.map((m, i) => _piCard(m, i, visible.length)).join('');
  if (tray) tray.innerHTML = _piTray();
}

function _piWeekBar() {
  const week = _piState.week || {};
  const options = _piState.weeks.map(w => {
    const label = w.label || w.id;
    const suffix = w.is_current ? ' · this week' : '';
    return `<option value="${escHtml(w.id)}"${w.id === week.id ? ' selected' : ''}>${escHtml(label)}${suffix}</option>`;
  }).join('');
  // A single week on record does not get a chooser — a select with one
  // option is a control that cannot do anything.
  const chooser = _piState.weeks.length > 1
    ? `<label class="pi-week-label" for="pi-week">Week</label>
       <select id="pi-week" class="input-w-lg" onchange="_piSelectWeek(this.value)">${options}</select>`
    : `<span class="pi-week-label">Week</span>
       <span class="pi-week-single">${escHtml(week.label || week.id || '')}</span>`;
  const meta = week.is_current
    ? (week.week_finished
        ? 'The newest one on record.'
        : 'This week is still going — these numbers move until Sunday.')
    : 'Looking back. The page opens on the newest week.';
  const count = _piState.weeks.length === 1
    ? '1 week on record'
    : `${_piState.weeks.length} weeks on record`;
  // The one place "this pod has no history yet" is said. Every card used to
  // append that sentence to its own headline, so a new pod's first page
  // repeated one line four times down the screen. The server hands it over
  // already worded (and already scored by the readability gate).
  const firstWeek = week.first_week_note
    ? `<div class="pi-week-note">${escHtml(week.first_week_note)}</div>`
    : '';
  return `${chooser}<span class="pi-week-meta">${escHtml(meta)} · ${escHtml(count)}</span>${firstWeek}`;
}

function _piCard(module, index, total) {
  const muted = module.critical && _piIsHidden(module);
  const rating = (_piState.profile.ratings || {})[module.module_id] || null;
  const id = attrJsLiteral(module.module_id);
  return `<section class="card pi-module${muted ? ' pi-module-muted' : ''}" data-module="${escHtml(module.module_id)}">
    <div class="pi-mod-top">
      <span class="card-title">${escHtml(module.title || '')}</span>
      ${_piTrendChip(module)}
    </div>
    <h2 class="pi-headline">${escHtml(module.headline || '')}</h2>
    ${muted ? `<div class="pi-sub">You turned this one down. It still shows because it is too important to hide.</div>` : `
      ${_piRemediation(module)}
      ${_piViz(module)}
      ${_piFacts(module)}
    `}
    ${_piDetail(module)}
    <div class="pi-foot">
      <span class="pi-hint">Was this useful?</span>
      <button type="button" class="btn btn-ghost btn-sm${rating === 'useful' ? ' pi-ctl-on' : ''}"
        aria-pressed="${rating === 'useful'}" onclick="_piRate(${id},'useful')">Useful</button>
      <button type="button" class="btn btn-ghost btn-sm${rating === 'not_useful' ? ' pi-ctl-on' : ''}"
        aria-pressed="${rating === 'not_useful'}" onclick="_piRate(${id},'not_useful')">Not really</button>
      ${_piIsHidden(module)
        ? `<button type="button" class="btn btn-ghost btn-sm" onclick="_piShow(${id})">Show</button>`
        : `<button type="button" class="btn btn-ghost btn-sm" onclick="_piHide(${id})"
             title="${module.critical
               ? 'This one is marked important, so hiding it turns it down rather than off — it keeps showing, quietly.'
               : 'Takes it off this page. The week it reports on is kept either way.'}">Hide</button>`}
      <button type="button" class="btn btn-ghost btn-sm pi-ctl-move" aria-label="Move up"
        ${index === 0 ? 'disabled' : ''} onclick="_piMove(${id},-1)">${_PI_CHEVRON_UP}</button>
      <button type="button" class="btn btn-ghost btn-sm pi-ctl-move" aria-label="Move down"
        ${index === total - 1 ? 'disabled' : ''} onclick="_piMove(${id},1)">${_PI_CHEVRON_DOWN}</button>
    </div>
  </section>`;
}

// A card that names a gap names the way to close it
// (docs/principle-alerts-explain-and-remediate.md). The sentence is the
// server's; this adds the door. NOT a command — the house rule is that the
// web UI never prints a privileged shell line for an operator to paste, so
// the link goes to the surface where the fix lives.
function _piRemediation(module) {
  const r = module.remediation;
  if (!r || !r.note) return '';
  const door = r.page
    ? ` <button type="button" class="btn btn-ghost btn-sm pi-fix-link"
         onclick="_piGoTo(${attrJsLiteral(r.page)})">Open Maintenance</button>`
    : '';
  return `<div class="pi-remediation"><span>${escHtml(r.note)}</span>${door}</div>`;
}

function _piGoTo(page) {
  // The SIDEBAR item, not any element carrying the attribute: nav() reads
  // dataset.page off what it is handed and activates it, so handing it a
  // non-nav node leaves the sidebar highlighting the wrong page.
  const el = document.querySelector(`.nav-item[data-page="${page}"]`);
  if (el) nav(el);
}

function _piTrendChip(module) {
  const chip = module.trend_chip || {};
  if (!chip.text) return '';
  // Neutral on purpose — routes_dossier explains why nothing here is
  // painted green or amber. The arrow carries direction; the color does
  // not carry a verdict the dossier never gave.
  const arrow = chip.direction === 'up' ? '↑'
    : chip.direction === 'down' ? '↓'
    : chip.direction === 'flat' ? '→' : '';
  return `<span class="badge badge-sm badge-neutral pi-trend">${arrow ? escHtml(arrow) + ' ' : ''}${escHtml(chip.text)}</span>`;
}

function _piFacts(module) {
  const facts = module.facts || [];
  if (!facts.length) return '';
  return `<div class="pi-facts">${facts.map(f => `
    <div class="pi-fact">
      <span class="pi-fact-label">${escHtml(f.label)}</span>
      ${f.measured
        ? `<b>${escHtml(f.value)}</b>`
        : `<b class="pi-fact-absent" title="Nothing on this pod has recorded this one yet. That is different from it being zero.">not measured</b>`}
    </div>`).join('')}</div>`;
}

function _piDetail(module) {
  const detail = module.detail || {};
  const rows = _piDetailRows(module);
  if (!rows.length && !Object.keys(detail).length) return '';
  return `<details class="pi-tech">
    <summary><span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>The detail behind this</summary>
    <div class="pi-tech-body">
      ${rows.length ? `<div class="resp-table-wrap"><table class="resp-table pi-tech-table">
        <thead><tr><th>Week</th><th>Value</th></tr></thead>
        <tbody>${rows}</tbody></table></div>` : ''}
      ${_piDetailDump(detail)}
    </div>
  </details>`;
}

// The chart's table twin (house dataviz rules: no value is reachable only
// by hovering a mark).
function _piDetailRows(module) {
  return (module.history || []).map(p => `<tr>
    <td data-label="Week">${escHtml(p.label)}</td>
    <td data-label="Value">${escHtml(p.value_display)}</td></tr>`).join('');
}

// The technical layer, verbatim. This is the ONE place field names are
// allowed on screen — the expanded detail is where D-T8 puts the depth,
// and rewriting it into English would be inventing words for numbers the
// synthesis never described.
function _piDetailDump(detail) {
  if (!detail || !Object.keys(detail).length) return '';
  return `<pre class="pi-tech-json">${escHtml(JSON.stringify(detail, null, 2))}</pre>`;
}

function _piTray() {
  const hidden = _piOrdered().filter(m => _piIsHidden(m) && !m.critical);
  if (!hidden.length) return '';
  return `<span class="pi-tray-label">Turned off</span>${hidden.map(m => `
    <span class="pi-tray-chip">${escHtml(m.title || m.module_id)}
      <button type="button" class="btn btn-ghost btn-sm" onclick="_piShow(${attrJsLiteral(m.module_id)})">Show</button>
    </span>`).join('')}`;
}

// ── the pictures ───────────────────────────────────────────────────────────
//
// House dataviz rules, and the shapes they rule out here:
//   * One series per chart, so there is no legend and no categorical
//     palette to validate — the card title says what is plotted.
//   * A one-bar bar chart is a number with extra ink, so the server sends
//     `bars: null` below two rows and the fact row carries it instead.
//   * A one-point line is not a trend, so a single week renders the
//     honest sentence instead of a chart.
//   * Thin marks, one direct label (the latest point), values in text
//     tokens — never in the data color.
//   * No gridlines and no axis on the trend line. A sparkline's job is the
//     SHAPE; unlabelled gridlines would be ink that carries nothing, and
//     labelling them would crowd a 116px-tall card. Every value stays
//     reachable: the end label, the per-point tooltip, and the table twin
//     inside "The detail behind this" — a tooltip never gates a number.

function _piViz(module) {
  if (!module.measurable) return '';
  const history = module.history || [];
  // THIS WEEK first, history second. A page whose only pictures need two
  // weeks of history shows an operator nothing on the day they install it —
  // which is exactly what the first shipped version of this page did. The
  // strip and the bars need no history at all.
  const now = module.strip ? _piDayStrip(module.strip)
    : module.bars ? _piBars(module.bars) : '';
  const over_time = history.length >= 2 ? _piSpark(history)
    : module.history_note
      ? `<div class="pi-viz pi-viz-absent">${escHtml(module.history_note)}</div>`
      : '';
  return now + over_time;
}

// The 28-day habit strip: one cell per day for the pod's leading scheduled
// app. Three states, and the third one matters — "not yet in use" is grey,
// because an app installed on Thursday did not miss Monday. Every cell's
// words are in its tooltip AND its aria-label, and the misses are listed in
// text under the strip: no value on this page is reachable only by hovering.
function _piDayStrip(strip) {
  const cells = (strip.cells || []).map(c =>
    `<span class="pi-day pi-day-${escHtml(c.state)}" title="${escHtml(c.tip)}"
       role="img" aria-label="${escHtml(c.tip)}"></span>`).join('');
  const legend = (strip.legend || []).map(l =>
    `<span class="pi-strip-key"><span class="pi-strip-dot pi-day-${escHtml(l.state)}"></span>${escHtml(l.text)}</span>`
  ).join('');
  const missed = strip.missed_label
    ? `<span class="pi-strip-key">${escHtml(strip.missed_label)}</span>` : '';
  return `<div class="pi-viz pi-strip-wrap">
    <div class="pi-daystrip" style="grid-template-columns:repeat(${(strip.cells || []).length},minmax(0,1fr))">${cells}</div>
    <div class="pi-strip-legend">${legend}${missed}
      ${strip.label ? `<span class="pi-strip-key pi-strip-subject">${escHtml(strip.label)}</span>` : ''}
    </div>
  </div>`;
}

function _piBars(bars) {
  const rows = bars.rows || [];
  const max = rows.reduce((m, r) => Math.max(m, Number(r.value) || 0), 0) || 1;
  return `<div class="pi-viz pi-bars">${rows.map(r => `
    <div class="pi-bar-row">
      <span class="pi-bar-name" title="${escHtml(r.label)}">${escHtml(r.label)}</span>
      <span class="pi-bar-track"><span class="pi-bar-fill" style="width:${((Number(r.value) || 0) / max * 100).toFixed(1)}%"></span></span>
      <span class="pi-bar-val">${escHtml(r.value_display)}</span>
    </div>`).join('')}
    <div class="pi-viz-note">${escHtml(bars.unit_label)}</div>
  </div>`;
}

function _piSpark(points) {
  // LABEL_PAD is the gutter the direct end-label lives in. Sized so the
  // widest amount we can render clears the viewBox: a label that runs off
  // the right edge is the "clipped label" anti-pattern, and cropping it is
  // worse than having none.
  const W = 560, H = 116, PAD = 10, LABEL_PAD = 78;
  const values = points.map(p => Number(p.value) || 0);
  const lo = Math.min.apply(null, values);
  const hi = Math.max.apply(null, values);
  // A flat series still needs a band, or every point lands on one pixel
  // row and the line reads as a missing chart.
  const span = (hi - lo) || (Math.abs(hi) || 1);
  const min = lo - span * 0.15, max = hi + span * 0.15;
  const x = i => PAD + i * (W - PAD - LABEL_PAD) / Math.max(points.length - 1, 1);
  const y = v => H - PAD - ((v - min) / (max - min || 1)) * (H - 2 * PAD);
  const pts = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const area = `${x(0).toFixed(1)},${H - PAD} ${pts} ${x(values.length - 1).toFixed(1)},${H - PAD}`;
  const dots = points.map((p, i) => {
    const last = i === points.length - 1;
    return `<circle cx="${x(i).toFixed(1)}" cy="${y(values[i]).toFixed(1)}" r="${last ? 5 : 4}"
      fill="var(--accent)" stroke="var(--bg2)" stroke-width="2"
      ><title>${escHtml(p.label)}: ${escHtml(p.value_display)}</title></circle>`;
  }).join('');
  const lastPoint = points[points.length - 1];
  const endLabel = `<text x="${(x(values.length - 1) + 9).toFixed(1)}" y="${(y(values[values.length - 1]) + 4).toFixed(1)}"
    font-size="12" font-weight="600" fill="var(--text)">${escHtml(lastPoint.value_display)}</text>`;
  return `<div class="pi-viz pi-spark">
    <svg viewBox="0 0 ${W} ${H}" role="img"
         aria-label="Week by week, ${escHtml(String(points.length))} weeks. Latest: ${escHtml(lastPoint.value_display)}.">
      <polygon points="${area}" fill="var(--accent)" fill-opacity="0.10"/>
      <polyline points="${pts}" fill="none" stroke="var(--accent)" stroke-width="2"
                stroke-linecap="round" stroke-linejoin="round"/>
      ${dots}${endLabel}
    </svg>
    <div class="pi-viz-note">${escHtml(points[0].label)} → ${escHtml(lastPoint.label)}</div>
  </div>`;
}

// Reorder arrows. Not .expand-icon — style-guide §9.13 rule 2 reserves that
// for expand/collapse, and these convey direction of a move.
const _PI_CHEVRON_UP = '<svg class="pi-move-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true"><polyline points="18 15 12 9 6 15"/></svg>';
const _PI_CHEVRON_DOWN = '<svg class="pi-move-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>';

window.intelligencePageActivate = intelligencePageActivate;
window._piSelectWeek = _piSelectWeek;
window._piRate = _piRate;
window._piHide = _piHide;
window._piShow = _piShow;
window._piMove = _piMove;
window._piGoTo = _piGoTo;

// ════════════════════════════════════════════════════════════════════════
// Page: Apps → Apps (the pod's defined apps) — AL-1.8a
//
// One row per app_id, across every bot. Columns (design §3):
//   name · purpose · bots (chips) · kind · last ran · cost 7d · status
//
// Reads GET /api/apps. The bot picker is a FILTER over the pod list, not a
// re-scope of it: narrowing to one bot still shows every bot each app is
// installed on, because "who else has this?" is precisely the question the
// old per-bot tab bar could not answer.
//
// Columns deliberately absent until their chip lands (brief §3): readiness
// and offer state (AL-1.6 / AL-1.7), delivery tri-state (AL-2.1), per-app
// caps (AL-2.3), audience editing + Access (P4), drift (B2). Where a value
// does not exist the cell says so — see _appsLastRunCell / _appsCostCell in
// apps-shell.js.
// ════════════════════════════════════════════════════════════════════════

let _appsListData = null;     // last /api/apps payload
let _appsFilterBot = '';
let _appsFilterKind = '';
let _appsFilterStatus = '';

async function appsLoadList() {
  const host = document.getElementById('apps-list-body');
  if (!host) return;
  host.innerHTML = `<div class="summary-band-loading"><div class="spinner" style="width:12px;height:12px;border-width:1.5px"></div> Loading the pod's apps…</div>`;
  let data;
  try {
    data = await api('GET', '/api/apps');
  } catch (err) {
    host.innerHTML = _appsErrorBox('the pod\'s apps', err);
    return;
  }
  if (!data || data.ok !== true) {
    host.innerHTML = _appsErrorBox('the pod\'s apps', data);
    return;
  }
  _appsListData = data;
  _appsRenderBotFilter();
  appsRenderList();
}

// The bot picker replaces the old tab bar. "All bots" is the default and
// the first option — pod-first means the pod view is what you land on.
function _appsRenderBotFilter() {
  const sel = document.getElementById('apps-filter-bot');
  if (!sel || !_appsListData) return;
  const bots = _appsListData.bots || [];
  sel.innerHTML = `<option value="">All bots</option>` + bots.map(b =>
    `<option value="${escHtml(b)}"${b === _appsFilterBot ? ' selected' : ''}>${escHtml(
      typeof botLabel === 'function' ? botLabel(b) : b)}</option>`
  ).join('');
}

function appsApplyFilters() {
  _appsFilterBot = document.getElementById('apps-filter-bot')?.value || '';
  _appsFilterKind = document.getElementById('apps-filter-kind')?.value || '';
  _appsFilterStatus = document.getElementById('apps-filter-status')?.value || '';
  appsRenderList();
}

// Clicking a bot chip in a row filters to that bot — the chip IS the
// navigation, so an operator who spots "team-bot-a" on a row can pivot to
// "everything team-bot-a has" without hunting for the picker.
function appsFilterByBot(botId) {
  _appsFilterBot = botId || '';
  const sel = document.getElementById('apps-filter-bot');
  if (sel) sel.value = _appsFilterBot;
  appsRenderList();
}

function _appsRowsAfterFilters() {
  const rows = (_appsListData && _appsListData.apps) || [];
  return rows.filter(a => {
    if (_appsFilterBot && !(a.bots || []).some(b => b.bot_id === _appsFilterBot)) return false;
    if (_appsFilterKind && a.kind !== _appsFilterKind) return false;
    if (_appsFilterStatus && a.status !== _appsFilterStatus) return false;
    return true;
  });
}

function appsRenderList() {
  const host = document.getElementById('apps-list-body');
  if (!host || !_appsListData) return;
  const rows = _appsRowsAfterFilters();
  const total = (_appsListData.apps || []).length;

  if (!total) {
    host.innerHTML = _appsEmpty(
      'No defined apps on this pod yet.',
      'Apps arrive three ways: the scanner discovers one on a bot and you promote it (see Discovered), you install one from the Gallery, or you author one with + New app.',
    );
    return;
  }
  if (!rows.length) {
    host.innerHTML = _appsEmpty(
      'No apps match these filters.',
      `${total} app${total === 1 ? '' : 's'} on the pod — clear the filters to see them.`,
    );
    return;
  }

  const unmeasured = (_appsListData.usage_unmeasured_bots || []);
  const coverage = unmeasured.length
    ? `<div style="font-size:0.75rem;color:var(--text3);margin-bottom:10px">
         Cost is measured on ${escHtml(String((_appsListData.bots || []).length - unmeasured.length))}
         of ${escHtml(String((_appsListData.bots || []).length))} bots.
         No per-app usage has been recorded yet for ${escHtml(unmeasured.map(b =>
           typeof botLabel === 'function' ? botLabel(b) : b).join(', '))} —
         cells for those bots say "not measured", which is not the same as zero.
       </div>`
    : '';

  host.innerHTML = coverage + `
    <div class="card" style="padding:0;overflow:hidden">
      <div class="resp-table-wrap">
        <table class="resp-table">
          <thead>
            <tr>
              <th>App</th>
              <th>Bots</th>
              <th>Runs</th>
              <th>Last ran</th>
              <th>Cost 7d</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(_appsListRow).join('')}
          </tbody>
        </table>
      </div>
    </div>
    <div style="font-size:0.75rem;color:var(--text3);margin-top:10px">
      Showing ${rows.length} of ${total} app${total === 1 ? '' : 's'} defined across the pod.
      Drafts the scanner has found but nobody has vouched for live under <b>Discovered</b>.
    </div>`;
}

function _appsListRow(a) {
  const purpose = a.purpose
    ? `<div style="font-size:0.78rem;color:var(--text2);max-width:420px;overflow:hidden;text-overflow:ellipsis">${escHtml(a.purpose)}</div>`
    : `<div style="font-size:0.78rem;color:var(--text3)">No purpose recorded yet</div>`;
  const kindChip = a.app_kind && a.app_kind !== 'application'
    ? ` <span class="badge badge-sm badge-neutral" title="${
        a.app_kind === 'capability'
          ? 'A reusable skill other apps build on, rather than a goal-shaped app.'
          : 'Pod infrastructure the bots run on, kept here so nothing is hidden.'
      }">${escHtml(a.app_kind)}</span>`
    : '';
  return `
    <tr class="apps-row" role="button" tabindex="0"
        onclick="appsShowDetail('${escHtml(a.app_id)}')"
        onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();appsShowDetail('${escHtml(a.app_id)}')}"
        style="cursor:pointer">
      <td data-label="App">
        <div style="font-weight:600">${escHtml(a.name || a.app_id)}${kindChip}</div>
        ${purpose}
      </td>
      <td data-label="Bots">${_appsBotChips(a.bots)}</td>
      <td data-label="Runs">${_appsKindLabel(a.kind)}</td>
      <td data-label="Last ran">${_appsLastRunCell(a.last_run)}</td>
      <td data-label="Cost 7d">${_appsCostCell(a.cost_7d, {
        measuredBots: a.usage_measured_bots, totalBots: a.bots_total,
      })}</td>
      <td data-label="Status">${_appsStatusBadge(a.status)}</td>
    </tr>`;
}

window.appsLoadList = appsLoadList;
window.appsRenderList = appsRenderList;
window.appsApplyFilters = appsApplyFilters;
window.appsFilterByBot = appsFilterByBot;

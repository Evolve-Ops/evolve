// ════════════════════════════════════════════════════════════════════════
// Page: Apps → App detail — AL-1.8a
//
// One view per app_id (design §4), three regions:
//
//   Header      name · purpose · app id · defined since · where it came
//               from · who it's for
//   On this pod the bots × facts table — for each bot that HAS the app:
//               version · what's installed · last ran (tri-state) ·
//               cost 7d with its grade split · actions. Plus the bots that
//               do NOT have it, behind a disabled "Install to…".
//   Files       (AL-1.8b, design §4a / D-U6) one row per file the APP has,
//               with a cell per bot saying what that bot's copy actually is
//   Uses        (AL-1.8b) what the app declares it needs — skills, tools,
//               integrations, credential NAMES
//   Signals     what the pod has noticed about this app, pulled from the
//               Signal store — not a separate page.
//
// THE FILES PANEL IS APP-FIRST ON PURPOSE. The old modal showed one BOT's
// file list, because that is what a manifest is. Here the file is the row
// and each bot is a column, so "team-bot-c is missing a file this app is
// supposed to have" is visible at a glance instead of being something you
// could only find by opening two modals and comparing them by eye.
//
// AND IT NEVER SAYS "ok" WITHOUT HAVING CHECKED. Six states, all from the
// route (see applications/app_files_view.py): ok · differs by the declared
// placeholders · differs, unexplained · missing · can't read · can't
// measure. The last two are muted rather than coloured, because "we could
// not look" is not a finding about the app.
//
// The old 90-field manifest modal survives EXACTLY as it was, one click
// behind "Open raw instance (advanced)" per bot. That is the whole of its
// role now: this view is the legible object, the modal is the raw record.
// Shrinking the modal is 1.4c, not this chip.
//
// Everything the action buttons call (viewManifest, archiveApp, …) lives in
// pages/apps.js and is reached as a global, the same way the Forge Jobs row
// and the Backup detail have always reached viewManifest.
// ════════════════════════════════════════════════════════════════════════

let _appsDetailData = null;

async function appsShowDetail(appId) {
  _appsDetailId = appId;
  const list = document.getElementById('apps-list-view');
  const detail = document.getElementById('apps-detail-view');
  if (!list || !detail) return;
  list.style.display = 'none';
  detail.style.display = '';
  detail.innerHTML = `<div class="summary-band-loading"><div class="spinner" style="width:12px;height:12px;border-width:1.5px"></div> Loading ${escHtml(appId)}…</div>`;
  let data;
  try {
    data = await api('GET', `/api/apps/${encodeURIComponent(appId)}`);
  } catch (err) {
    detail.innerHTML = _appsBackLink() + _appsErrorBox('this app', err);
    return;
  }
  if (!data || data.ok !== true) {
    detail.innerHTML = _appsBackLink() + _appsErrorBox('this app', data);
    return;
  }
  _appsDetailData = data;
  _appsRenderDetail();
}

function appsCloseDetail() {
  _appsDetailId = null;
  _appsDetailData = null;
  const list = document.getElementById('apps-list-view');
  const detail = document.getElementById('apps-detail-view');
  if (detail) { detail.style.display = 'none'; detail.innerHTML = ''; }
  if (list) list.style.display = '';
  appsLoadList();
}

function _appsBackLink() {
  return `<button class="btn btn-ghost btn-sm" onclick="appsCloseDetail()" style="margin-bottom:14px">← All apps</button>`;
}

function _appsProvenanceLine(a) {
  const p = a.provenance || {};
  const origin = String(p.origin || '').toLowerCase();
  if (origin === 'discovered') {
    return p.from_bot
      ? `Found on ${escHtml(typeof botLabel === 'function' ? botLabel(p.from_bot) : p.from_bot)} by the scanner, then vouched for`
      : 'Found by the scanner, then vouched for';
  }
  if (origin === 'imported') {
    return p.from_pod ? `Imported from ${escHtml(p.from_pod)}` : 'Imported from another pod';
  }
  if (origin === 'authored') return 'Authored here';
  return 'Where this came from was not recorded';
}

function _appsRenderDetail() {
  const host = document.getElementById('apps-detail-view');
  const a = _appsDetailData;
  if (!host || !a) return;

  const audience = a.audience === 'primary_user'
    ? 'Just its primary user'
    : a.audience === 'admins' ? 'Pod admins' : 'Everyone on the bot';

  host.innerHTML = `
    ${_appsBackLink()}
    <div class="card" style="margin-bottom:16px">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:14px;flex-wrap:wrap">
        <div style="min-width:240px">
          <h2 style="margin:0 0 4px">${escHtml(a.name || a.app_id)}</h2>
          <div style="font-size:0.85rem;color:var(--text2);max-width:64ch">${
            a.purpose
              ? escHtml(a.purpose)
              : '<span style="color:var(--text3)">No purpose recorded yet — the one line that tells a bot when to reach for this app.</span>'
          }</div>
        </div>
        <div style="text-align:right;font-size:0.75rem;color:var(--text3);line-height:1.7">
          <div><code>${escHtml(a.app_id)}</code></div>
          <div>${a.defined_since ? 'Defined ' + escHtml(ago(a.defined_since)) : 'Defined date not recorded'}</div>
          <div>${_appsProvenanceLine(a)}</div>
          <div>For: ${escHtml(audience)}</div>
        </div>
      </div>
      <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        ${_appsStatusBadge(a.status)}
        <span class="badge badge-sm badge-neutral">${_appsKindLabel(a.kind)}</span>
        <span class="badge badge-sm badge-neutral" title="${
          a.spec_source === 'vnext'
            ? 'This app has a written spec on the pod — the portable description an install or a share carries.'
            : 'No written spec on the pod yet; this is read from what is installed on the bots.'
        }">${escHtml(_appsSpecVersionLabel(a.spec_version))}${a.spec_source === 'vnext' ? '' : ' · read from the bots'}</span>
      </div>
    </div>

    <h3 style="margin:0 0 8px;font-size:0.95rem;font-weight:700">On this pod</h3>
    <div class="card" style="padding:0;overflow:hidden;margin-bottom:8px">
      <div class="resp-table-wrap">
        <table class="resp-table">
          <thead>
            <tr>
              <th>Bot</th>
              <th>Version</th>
              <th>What's installed</th>
              <th>Last ran</th>
              <th>Cost 7d</th>
              <th>State</th>
              <th></th>
            </tr>
          </thead>
          <tbody>${(a.bots || []).map(b => _appsBotFactsRow(a, b)).join('')}</tbody>
        </table>
      </div>
    </div>
    ${_appsBotsWithout(a)}

    <h3 style="margin:22px 0 8px;font-size:0.95rem;font-weight:700">Files</h3>
    ${_appsFilesBlock(a)}

    <h3 style="margin:22px 0 8px;font-size:0.95rem;font-weight:700">What this app uses</h3>
    ${_appsUsesBlock(a)}

    <h3 style="margin:22px 0 8px;font-size:0.95rem;font-weight:700">Signals about this app</h3>
    ${_appsSignalsBlock(a)}`;
}

function _appsBotFactsRow(a, b) {
  const label = typeof botLabel === 'function' ? botLabel(b.bot_id) : b.bot_id;
  const defined = (a.definition_states || {})[b.bot_id] === 'defined';
  const grades = _appsGradeBadges(b.grade_breakdown);
  const cost = b.usage_measured
    ? _appsCostCell(b.cost_7d)
    : `<span style="color:var(--text3)" title="No per-app usage has been recorded for this bot at all — the daily rollup has not run here. 'Not measured', not 'not used'.">not measured</span>`;
  return `
    <tr>
      <td data-label="Bot"><b>${escHtml(label)}</b></td>
      <td data-label="Version">${escHtml(_appsSpecVersionLabel(b.spec_version))}</td>
      <td data-label="What's installed">${escHtml(b.config_summary)}</td>
      <td data-label="Last ran">${_appsLastRunCell(b.last_run)}</td>
      <td data-label="Cost 7d">${cost}${grades ? `<div style="margin-top:4px">${grades}</div>` : ''}</td>
      <td data-label="State">${
        defined
          ? '<span class="badge badge-sm badge-ok" title="Someone vouched for this app on this bot: the manifest is the authoritative description.">Defined</span>'
          : '<span class="badge badge-sm badge-neutral" title="Found by the scanner on this bot but not vouched for yet. Promote it from Discovered.">Discovered</span>'
      }</td>
      <td data-label="">
        <button class="btn btn-ghost btn-sm"
          onclick="viewManifest('${escHtml(b.bot_id)}','${escHtml(b.manifest_stem)}','${escHtml(b.manifest_stem)}')"
          title="The full record this bot keeps for the app — every field, as stored. Advanced.">Open raw instance</button>
      </td>
    </tr>`;
}

function _appsBotsWithout(a) {
  const missing = a.bots_without || [];
  if (!missing.length) {
    return `<div style="font-size:0.75rem;color:var(--text3)">Every bot on the pod has this app.</div>`;
  }
  const names = missing.map(b => typeof botLabel === 'function' ? botLabel(b) : b);
  return `
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:0.78rem;color:var(--text2)">
      <span>Not on ${escHtml(names.join(', '))}.</span>
      ${_appsInstallToButton('Install to…')}
    </div>`;
}

function _appsSignalsBlock(a) {
  const sigs = a.signals || [];
  if (!sigs.length) {
    return _appsEmpty(
      'Nothing has been flagged about this app.',
      'Delivery failures, cost-cap trips and audit findings that name this app would appear here.',
    );
  }
  return `
    <div class="card" style="padding:0;overflow:hidden">
      <div class="resp-table-wrap">
        <table class="resp-table">
          <thead><tr><th>What</th><th>Bot</th><th>Severity</th><th>Last seen</th></tr></thead>
          <tbody>${sigs.map(s => `
            <tr>
              <td data-label="What">${escHtml(s.title || s.type)}</td>
              <td data-label="Bot">${escHtml(s.bot_id ? (typeof botLabel === 'function' ? botLabel(s.bot_id) : s.bot_id) : 'pod')}</td>
              <td data-label="Severity">${
                s.severity === 'alert' ? '<span class="badge badge-sm badge-crit">Alert</span>'
                : s.severity === 'warn' ? '<span class="badge badge-sm badge-warn">Warning</span>'
                : '<span class="badge badge-sm badge-neutral">Info</span>'
              }</td>
              <td data-label="Last seen">${escHtml(ago(s.last_observed_at))}</td>
            </tr>`).join('')}</tbody>
        </table>
      </div>
    </div>`;
}



// ── Files (design §4a / D-U6) ──────────────────────────────────────────────

// What the digest column IS. The same field carries two different things
// depending on where it came from (a shared package's original bytes, or
// one bot's own copy), and the pod does not always record which — so the
// column says which one it is showing rather than printing a hex string
// that could be either.
function _appsDigestHeading(pkg) {
  const kind = pkg && pkg.sha_kind;
  if (kind === 'source') return ['Original digest', 'Taken from the shared package this app was installed from, before each bot\'s copy was personalised.'];
  if (kind === 'realized') {
    const bot = pkg.sha_kind_bot
      ? (typeof botLabel === 'function' ? botLabel(pkg.sha_kind_bot) : pkg.sha_kind_bot)
      : 'a bot';
    return [`Digest (${bot}'s copy)`, `No shared package exists for this app, so the app's digest is the one taken from ${bot}'s own copy. Other bots are compared against that.`];
  }
  if (kind === 'recorded') return ['Recorded digest', 'The digest written down for this app. This pod does not record whether it came from a shared package or from one bot\'s own copy.'];
  return ['Digest', 'No digest has been recorded for these files.'];
}

const _APPS_FILE_STATES = {
  ok: ['ok', 'badge-ok',
       'This bot\'s copy is identical to the app\'s recorded version.'],
  differs_placeholder: ['differs — personalised', 'badge-neutral',
       'This bot\'s copy differs only by the substitutions the package declares (its own name, its own folder). Checked by re-doing that substitution and getting exactly this copy back — not assumed.'],
  differs: ['differs', 'badge-warn',
       'This bot\'s copy is not the app\'s recorded version, and nothing on the pod explains the difference. Someone or something changed it here.'],
  missing: ['missing', 'badge-warn',
       'The app says it has this file; this bot does not have it.'],
  cant_read: ['can\'t read', 'badge-neutral',
       'The file is there but could not be read, or the path is a folder rather than a file.'],
  cant_measure: ['can\'t measure', 'badge-neutral',
       'Nothing here can be compared — see the note on the cell.'],
};

function _appsFileStateCell(cell) {
  const c = cell || {};
  const meta = _APPS_FILE_STATES[c.state] || ['unknown', 'badge-neutral', ''];
  const title = c.note ? `${meta[2]}\n\n${c.note}` : meta[2];
  // The two "we could not look" states are muted text, not a coloured badge:
  // an absence of measurement must not read as a finding about the app
  // (principle-tri-state-status).
  if (c.state === 'cant_measure' || c.state === 'cant_read') {
    return `<span style="color:var(--text3);font-size:0.72rem" title="${escHtml(title)}">${meta[0]}</span>`;
  }
  return `<span class="badge badge-sm ${meta[1]}" title="${escHtml(title)}">${meta[0]}</span>`;
}

// A digest is 64 hex characters; eight of them is enough to recognise one
// and short enough to sit in a table. The full value is in the tooltip.
function _appsShaChip(sha, help) {
  if (!sha) {
    return `<span style="color:var(--text3)" title="No digest has been recorded for this file, so there is nothing to compare a bot's copy against. That is 'not recorded', not 'the file is empty'.">not hashed</span>`;
  }
  return `<code style="font-size:0.72rem" title="${escHtml(sha + (help ? '\n\n' + help : ''))}">${escHtml(String(sha).slice(0, 8))}</code>`;
}

function _appsFilesBlock(a) {
  const pkg = a.package;
  if (!pkg) {
    return _appsEmpty(
      'The file list could not be read.',
      'The rest of this page is what the pod could answer; this panel is blank rather than showing a list it is not sure of.',
    );
  }
  const files = pkg.files || [];
  if (!files.length) {
    return _appsEmpty(
      'This app does not list any files.',
      'Some apps are standing instructions rather than scripts — they run from what the bot has been told, with nothing on disk to point at.',
    );
  }
  const bots = (a.bots || []).map(b => b.bot_id);
  const [digestLabel, digestHelp] = _appsDigestHeading(pkg);
  const heads = bots.map(b => `<th>${escHtml(
    typeof botLabel === 'function' ? botLabel(b) : b)}</th>`).join('');
  return `
    <div class="card" style="padding:0;overflow:hidden">
      <div class="resp-table-wrap">
        <table class="resp-table">
          <thead>
            <tr>
              <th>File</th>
              <th>What it is</th>
              <th title="${escHtml(digestHelp)}">${escHtml(digestLabel)}</th>
              ${heads}
            </tr>
          </thead>
          <tbody>${files.map(f => `
            <tr>
              <td data-label="File"><code style="font-size:0.72rem">${escHtml(f.path)}</code></td>
              <td data-label="What it is">${f.role
                ? escHtml(_appsFileRoleLabel(f.role))
                : '<span style="color:var(--text3)">not recorded</span>'}</td>
              <td data-label="${escHtml(digestLabel)}">${_appsShaChip(f.sha256, digestHelp)}</td>
              ${bots.map(b => `<td data-label="${escHtml(
                typeof botLabel === 'function' ? botLabel(b) : b)}">${
                _appsFileStateCell((f.bots || {})[b])}</td>`).join('')}
            </tr>`).join('')}</tbody>
        </table>
      </div>
    </div>
    <div style="font-size:0.75rem;color:var(--text3);margin-top:10px">
      ${files.length} file${files.length === 1 ? '' : 's'} ·
      ${pkg.hashed} with a recorded digest.
      Files belong to the app; each column is what one bot actually has.
    </div>`;
}

// The blueprint role vocabulary, in words. Anything else is shown as it was
// recorded rather than guessed at.
function _appsFileRoleLabel(role) {
  const map = {
    vital_to_blueprint: 'core to the app',
    instance_specific: 'specific to this bot',
    reference_only: 'reference',
  };
  return map[role] || role;
}

// ── Uses (design §4a) ──────────────────────────────────────────────────────

function _appsUsesBlock(a) {
  const requires = a.requires;
  // Absent is not empty. No spec at all -> we cannot say; a spec that
  // declares nothing -> it declares nothing. Two different sentences.
  if (!requires) {
    return _appsEmpty(
      'We don\'t have a description of what this app uses.',
      'That description arrives when the app is written down as a shareable spec — until then this is blank rather than empty.',
    );
  }
  const groups = [
    ['skills', 'Skills', 'Skills the bot needs for this app to work.'],
    ['tools', 'Tools', 'Tools this app calls while it runs.'],
    ['integrations', 'Integrations', 'Outside services this app talks to.'],
    ['credentials', 'Credentials', 'The names of the credentials this app needs. Names only — no values are shown here or sent to this page.'],
  ];
  const values = {
    skills: requires.skills || [],
    tools: requires.tools || [],
    integrations: requires.integrations || [],
    credentials: requires.secrets || [],
  };
  const exclusive = a.exclusive_tools || [];
  if (!Object.values(values).some(v => v.length) && !exclusive.length) {
    return _appsEmpty(
      'Nothing declared.',
      'This app says it needs no skills, tools, integrations or credentials.',
    );
  }
  const rows = groups.filter(([key]) => values[key].length || (key === 'tools' && exclusive.length))
    .map(([key, label, help]) => {
      const chips = values[key].map(v =>
        `<span class="badge badge-sm badge-neutral">${escHtml(v)}</span>`).join(' ');
      const exclusiveChips = key === 'tools'
        ? exclusive.map(t => `<span class="badge badge-sm badge-neutral"
            title="Only this app uses this tool on the pod.">${escHtml(t)} · exclusively</span>`).join(' ')
        : '';
      return `
        <div style="display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-top:1px solid var(--border)">
          <div style="min-width:130px;font-size:0.75rem;color:var(--text2)" title="${escHtml(help)}">${escHtml(label)}</div>
          <div style="display:flex;gap:6px;flex-wrap:wrap">${chips}${
            chips && exclusiveChips ? ' ' : ''}${exclusiveChips}</div>
        </div>`;
    }).join('');
  return `<div class="card" style="padding:4px 16px 12px">${rows}</div>`;
}

window.appsShowDetail = appsShowDetail;
window.appsCloseDetail = appsCloseDetail;

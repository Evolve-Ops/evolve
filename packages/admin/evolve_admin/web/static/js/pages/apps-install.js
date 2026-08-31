// ════════════════════════════════════════════════════════════════════════
// Page: Apps → Install to… / Update to vN — AL-3.2
//
// internal/dispatch/done/al-3-2-install-to.md ·
// internal/design-apps-surface-2026-08-16.md §4/§5
//
// One dialog, two acts, because they are the same conversation in two
// tenses: "put this app on that bot" and "bring that bot's copy up to
// date". Both follow the same shape, and the shape is the point —
//
//     pick  →  PREVIEW (writes nothing)  →  the operator reads it  →  apply
//
// The preview is not a courtesy. Both routes default to a dry run, and the
// dry run does the whole computation — resolves the packaged copy, works
// out every file, predicts every digest — so what the dialog shows is what
// the apply will do, not a guess about it.
//
// WHAT THE DIALOG SAYS, IN WORDS AND NOT FIELD NAMES (design §7, the Plex
// test): "packaged copy" not files-pack, "digest" not sha256, "changed on
// this bot" not adapted. The only hex on screen is eight characters of a
// digest with the whole of it in the tooltip, because recognising one is
// useful and reading one is not.
//
// UPDATE IS A MERGE, AND THE DIALOG IS WHERE THAT BECOMES VISIBLE (D-L3).
// Where a file was changed on the target bot, the update refuses by
// default and this dialog shows which files and what would be lost. Going
// ahead needs a deliberate tick — not a second click on the same button —
// and even then the server re-checks each file against the digest this
// preview measured, so a tick given about one state cannot apply to
// another.
// ════════════════════════════════════════════════════════════════════════

// The open dialog's state. `preview` is the last dry-run payload — the
// apply is only ever offered against one we actually have.
let _appsInstall = null;

function _appsInstallModal() {
  return document.getElementById('app-install-modal');
}

function _appsInstallBody() {
  return document.getElementById('app-install-modal-body');
}

function _appsInstallTitle(text) {
  const el = document.getElementById('app-install-modal-title');
  if (el) el.textContent = text;
}

// Esc + overlay click + the X button — all three, per the style guide's
// modal rules. The key handler is torn down on close so a closed dialog
// never eats an Escape meant for something else.
function _appsInstallOnKey(e) {
  if (e.key === 'Escape') { e.preventDefault(); appsCloseInstall(); }
}

function _appsInstallOpenShell(title) {
  const modal = _appsInstallModal();
  if (!modal) return false;
  _appsInstallTitle(title);
  modal.classList.add('open');
  document.addEventListener('keydown', _appsInstallOnKey);
  modal.onclick = (e) => { if (e.target === modal) appsCloseInstall(); };
  return true;
}

function appsCloseInstall() {
  const modal = _appsInstallModal();
  if (modal) { modal.classList.remove('open'); modal.onclick = null; }
  document.removeEventListener('keydown', _appsInstallOnKey);
  _appsInstall = null;
}

function _appsInstallBusy(message) {
  const body = _appsInstallBody();
  if (body) {
    body.innerHTML = `<div class="summary-band-loading"><div class="spinner" style="width:12px;height:12px;border-width:1.5px"></div> ${escHtml(message)}</div>`;
  }
}

function _appsInstallError(where, err) {
  const msg = (err && (err.error || err.message)) || 'the request failed';
  const body = _appsInstallBody();
  if (body) {
    body.innerHTML = `
      <div class="empty">Couldn't ${escHtml(where)} — ${escHtml(String(msg))}.
        <div style="font-size:0.78rem;color:var(--text3);margin-top:6px">Nothing was written. The message above is what the pod actually reported.</div>
      </div>
      <div style="display:flex;justify-content:flex-end;margin-top:14px">
        <button class="btn btn-ghost" onclick="appsCloseInstall()">Close</button>
      </div>`;
  }
}

function _appsBotName(botId) {
  return typeof botLabel === 'function' ? botLabel(botId) : botId;
}

// ── Install ────────────────────────────────────────────────────────────────

function appsOpenInstall() {
  const a = _appsDetailData;
  if (!a || !_appsInstallOpenShell(`Install ${a.name || a.app_id} on another bot`)) return;
  _appsInstall = {
    mode: 'install', appId: a.app_id, botId: '',
    sourceBot: '', preview: null, confirm: false,
  };
  _appsRenderInstallPicker();
}

function _appsRenderInstallPicker() {
  const a = _appsDetailData;
  const body = _appsInstallBody();
  if (!a || !body || !_appsInstall) return;
  const targets = a.bots_without || [];
  const install = a.install || {};
  const sources = install.sources || [];

  if (!targets.length) {
    body.innerHTML = _appsEmpty(
      'Every bot on the pod already has this app.',
      'To move one of them to a newer version, use Update on its row.',
    ) + `<div style="display:flex;justify-content:flex-end;margin-top:14px">
      <button class="btn btn-ghost" onclick="appsCloseInstall()">Close</button></div>`;
    return;
  }

  // The source picker appears only when the choice is real. Two bots'
  // copies of one app are two different sets of bytes, so with more than
  // one the pod refuses to pick — and the dialog has to ask rather than
  // send a request it knows will come back asking.
  const sourceRow = sources.length > 1 ? `
    <div style="margin-top:14px">
      <div style="font-size:0.75rem;color:var(--text2);margin-bottom:4px"
        title="This app exists on more than one bot and their copies are not identical. Which one should the packaged copy be taken from?">Copy it from</div>
      <select id="app-install-source" class="input-w-lg" onchange="_appsInstallPickSource(this.value)">
        <option value="">Choose a bot…</option>
        ${sources.map(b => `<option value="${escHtml(b)}">${escHtml(_appsBotName(b))}</option>`).join('')}
      </select>
    </div>` : '';

  const note = install.state === 'snapshot_needed' ? `
    <div style="font-size:0.78rem;color:var(--text2);background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:10px 12px;margin-top:14px">
      This app has no packaged copy yet. One is taken from the bot that has it first — that step only reads, and you will see the file list before anything is installed.
    </div>` : '';

  body.innerHTML = `
    <div style="font-size:0.82rem;color:var(--text2);margin-bottom:4px">
      The same files land on the new bot, with its own name and folders substituted in. Nothing is written until you have seen the list.
    </div>
    <div style="margin-top:14px">
      <div style="font-size:0.75rem;color:var(--text2);margin-bottom:4px">Install on</div>
      <select id="app-install-target" class="input-w-lg" onchange="_appsInstallPickTarget(this.value)">
        <option value="">Choose a bot…</option>
        ${targets.map(b => `<option value="${escHtml(b)}">${escHtml(_appsBotName(b))}</option>`).join('')}
      </select>
    </div>
    ${sourceRow}
    ${note}
    <div id="app-install-plan"></div>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px;padding-top:12px;border-top:1px solid var(--border)">
      <button class="btn btn-ghost" onclick="appsCloseInstall()">Cancel</button>
    </div>`;
}

function _appsInstallPickSource(value) {
  if (!_appsInstall) return;
  _appsInstall.sourceBot = value || '';
  if (_appsInstall.botId) _appsInstallPreview();
}

function _appsInstallPickTarget(value) {
  if (!_appsInstall) return;
  _appsInstall.botId = value || '';
  _appsInstall.preview = null;
  const plan = document.getElementById('app-install-plan');
  if (plan) plan.innerHTML = '';
  if (value) _appsInstallPreview();
}

async function _appsInstallPreview() {
  const state = _appsInstall;
  if (!state || !state.botId) return;
  const plan = document.getElementById('app-install-plan');
  if (plan) {
    plan.innerHTML = `<div class="summary-band-loading" style="margin-top:12px"><div class="spinner" style="width:12px;height:12px;border-width:1.5px"></div> Working out what would be installed…</div>`;
  }
  let data;
  try {
    data = await api('POST', `/api/apps/${encodeURIComponent(state.appId)}/install`, {
      bot_id: state.botId, source_bot: state.sourceBot, dry_run: true,
    });
  } catch (err) {
    _appsInstallError('check this install', err);
    return;
  }
  if (_appsInstall !== state) return;    // the dialog moved on while we waited
  if (!data || data.ok !== true) { _appsRenderInstallBlocked(data); return; }
  state.preview = data;
  _appsRenderInstallPlan(data);
}

// A refusal is rendered where the plan would have been, with the pod's own
// sentence — not a generic failure. These are the states an operator can
// act on (a name collision, an app with nothing to install), and rewriting
// them into "something went wrong" would throw away the remediation.
function _appsRenderInstallBlocked(data) {
  const plan = document.getElementById('app-install-plan');
  if (!plan) return;
  const msg = (data && data.error) || 'the pod could not answer';
  plan.innerHTML = `
    <div style="font-size:0.8rem;color:var(--text);background:var(--bg3);border:1px solid var(--border);border-left:3px solid var(--red);border-radius:6px;padding:10px 12px;margin-top:14px">
      <div style="font-weight:600;margin-bottom:4px">This install can't go ahead</div>
      <div style="color:var(--text2);line-height:1.5">${escHtml(String(msg))}</div>
    </div>`;
}

function _appsInstallFileRows(rows) {
  return `
    <div class="resp-table-wrap" style="max-height:260px;overflow-y:auto">
      <table class="resp-table">
        <thead><tr><th>File</th><th>What it will be</th></tr></thead>
        <tbody>${rows.map(f => `
          <tr>
            <td data-label="File"><code style="font-size:0.72rem">${escHtml(f.rel)}</code></td>
            <td data-label="What it will be">${_appsInstallDigest(f)}</td>
          </tr>`).join('')}</tbody>
      </table>
    </div>`;
}

// Eight characters of the digest, the whole of it in the tooltip, and a
// word for whether this bot's copy will differ from the packaged one —
// which it does exactly where the app declares something to substitute.
function _appsInstallDigest(f) {
  const same = f.source_sha && f.predicted_sha === f.source_sha;
  const label = same ? 'identical to the packaged copy'
                     : 'personalised for this bot';
  return `<code style="font-size:0.72rem" title="${escHtml(f.predicted_sha || '')}">${escHtml(String(f.predicted_sha || '').slice(0, 8))}</code>
    <span style="color:var(--text3);font-size:0.72rem;margin-left:6px">${escHtml(label)}</span>`;
}

function _appsRenderInstallPlan(data) {
  const plan = document.getElementById('app-install-plan');
  if (!plan) return;
  if (data.needs_snapshot) {
    plan.innerHTML = `
      <div style="font-size:0.8rem;color:var(--text2);background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:10px 12px;margin-top:14px">
        A packaged copy will be taken from ${escHtml(_appsBotName((data.snapshot || {}).bot_id || ''))} first, then installed. The file list is shown after that step, because there is nothing to list until the copy exists.
      </div>
      <div style="display:flex;justify-content:flex-end;margin-top:12px">
        <button class="btn btn-primary" onclick="appsApplyInstall()">Package and install</button>
      </div>`;
    return;
  }
  const files = data.planned || [];
  plan.innerHTML = `
    <div style="font-size:0.78rem;color:var(--text2);margin:14px 0 6px">
      ${files.length} file${files.length === 1 ? '' : 's'} would be written to ${escHtml(_appsBotName(data.bot_id))}. None of them exist there now — an install creates files, it never writes over one.
    </div>
    ${_appsInstallFileRows(files)}
    <div style="display:flex;justify-content:flex-end;margin-top:12px">
      <button class="btn btn-primary" onclick="appsApplyInstall()">Install on ${escHtml(_appsBotName(data.bot_id))}</button>
    </div>`;
}

async function appsApplyInstall() {
  const state = _appsInstall;
  if (!state || !state.preview) return;
  _appsInstallBusy(`Installing on ${_appsBotName(state.botId)}…`);
  let data;
  try {
    data = await api('POST', `/api/apps/${encodeURIComponent(state.appId)}/install`, {
      bot_id: state.botId, source_bot: state.sourceBot, dry_run: false,
    });
  } catch (err) {
    _appsInstallError('install this app', err);
    return;
  }
  if (!data || data.ok !== true) { _appsRenderInstallResult(data, false); return; }
  _appsRenderInstallResult(data, true);
}

// The result is rendered IN the dialog rather than toasted away, because a
// partial install has per-file detail an operator needs, and a toast is the
// wrong place for a list.
function _appsRenderInstallResult(data, ok) {
  const body = _appsInstallBody();
  const state = _appsInstall;
  if (!body) return;
  if (ok) {
    const n = (data.installed || []).length;
    toast(`✓ Installed on ${_appsBotName(data.bot_id)}`, 'ok');
    body.innerHTML = `
      <div style="font-size:0.85rem;margin-bottom:8px">Installed on <b>${escHtml(_appsBotName(data.bot_id))}</b> — ${n} file${n === 1 ? '' : 's'}.</div>
      <div style="font-size:0.78rem;color:var(--text2);line-height:1.5">
        Every file was checked after it was written: what landed is exactly what the packaged copy plus this bot's own details produce, and nothing else.
      </div>
      <div style="display:flex;justify-content:flex-end;margin-top:16px">
        <button class="btn btn-primary" onclick="appsCloseInstall();appsShowDetail('${escHtml(data.app_id)}')">Done</button>
      </div>`;
    return;
  }
  const failed = (data && data.failed) || [];
  const landed = (data && data.installed) || [];
  body.innerHTML = `
    <div style="font-size:0.8rem;color:var(--text);background:var(--bg3);border:1px solid var(--border);border-left:3px solid var(--red);border-radius:6px;padding:10px 12px">
      <div style="font-weight:600;margin-bottom:4px">The install did not complete</div>
      <div style="color:var(--text2);line-height:1.5">${escHtml((data && data.error) || 'the request failed')}</div>
    </div>
    ${failed.length ? `
      <div style="font-size:0.75rem;color:var(--text2);margin:12px 0 4px">Files that could not be written</div>
      <div class="resp-table-wrap" style="max-height:200px;overflow-y:auto">
        <table class="resp-table">
          <thead><tr><th>File</th><th>Why</th></tr></thead>
          <tbody>${failed.map(f => `
            <tr><td data-label="File"><code style="font-size:0.72rem">${escHtml(f.rel)}</code></td>
                <td data-label="Why" style="font-size:0.75rem;color:var(--text2)">${escHtml(f.error || '')}</td></tr>`).join('')}</tbody>
        </table>
      </div>` : ''}
    ${landed.length ? `
      <div style="font-size:0.75rem;color:var(--text3);margin-top:10px">
        ${landed.length} file${landed.length === 1 ? '' : 's'} did land and ${landed.length === 1 ? 'is' : 'are'} still on the bot: ${landed.map(p => `<code style="font-size:0.72rem">${escHtml(p)}</code>`).join(' ')}. The app was not recorded as installed, so the pod is not claiming it is there.
      </div>` : ''}
    <div style="display:flex;justify-content:flex-end;margin-top:16px">
      <button class="btn btn-ghost" onclick="appsCloseInstall()">Close</button>
    </div>`;
  if (state) state.preview = null;
}

// ── Update ─────────────────────────────────────────────────────────────────

function appsOpenUpdate(botId) {
  const a = _appsDetailData;
  if (!a || !_appsInstallOpenShell(
    `Update ${a.name || a.app_id} on ${_appsBotName(botId)}`)) return;
  _appsInstall = {
    mode: 'update', appId: a.app_id, botId: botId,
    sourceBot: '', preview: null, confirm: false,
  };
  _appsInstallBusy('Comparing this bot’s copy with the current version…');
  _appsUpdatePreview();
}

async function _appsUpdatePreview() {
  const state = _appsInstall;
  if (!state) return;
  let data;
  try {
    data = await api('POST', `/api/apps/${encodeURIComponent(state.appId)}/update`, {
      bot_id: state.botId, dry_run: true,
    });
  } catch (err) {
    _appsInstallError('check this update', err);
    return;
  }
  if (_appsInstall !== state) return;
  if (!data || data.ok !== true) { _appsInstallError('check this update', data); return; }
  state.preview = data;
  _appsRenderUpdatePlan(data);
}

// The four plan states, in words. `create` and `unadapted` are ordinary;
// `adapted` is the one the operator has to decide about, so it gets the
// list and the tick rather than a badge in a table nobody reads.
const _APPS_UPDATE_WORDS = {
  create: ['new in this version', 'This version adds a file the bot has not got.'],
  unadapted: ['up to date after this', 'Untouched on this bot since it was installed, so replacing it loses nothing.'],
  adapted: ['changed on this bot', 'This file is not what was installed. Replacing it would discard the change.'],
  collision: ['already there', 'A file is at this path and the pod cannot say what put it there.'],
};

function _appsUpdateStateCell(item) {
  const meta = _APPS_UPDATE_WORDS[item.state] || ['unknown', ''];
  const note = item.note ? `${meta[1]}\n\n${item.note}` : meta[1];
  const cls = item.state === 'adapted' ? 'badge-warn' : 'badge-neutral';
  return `<span class="badge badge-sm ${cls}" title="${escHtml(note)}">${escHtml(meta[0])}</span>`;
}

function _appsRenderUpdatePlan(data) {
  const body = _appsInstallBody();
  if (!body) return;
  const items = data.plan || [];
  const conflicts = data.conflicts || [];
  const dropped = data.removed_upstream || [];
  const moving = items.filter(i => i.state !== 'unadapted' || i.current_sha !== i.predicted_sha);

  // The honest-basis line. The strong check compares against the digest
  // recorded when this bot's copy was installed; the fallback re-derives
  // from the packaged copy and cannot tell a local edit from a change in
  // the new version. Which one was used is stated rather than implied.
  const weak = (data.bases || []).indexOf('current_pack') >= 0;
  const basis = weak ? `
    <div style="font-size:0.75rem;color:var(--text3);margin-top:8px">
      This bot's copy does not record what was installed, so "changed on this bot" is worked out by re-deriving the files from the packaged copy. That cannot tell a change made here from a change in the new version — anything unclear is treated as changed here.
    </div>` : '';

  body.innerHTML = `
    <div style="font-size:0.82rem;color:var(--text2)">
      ${escHtml(_appsBotName(data.bot_id))} is on ${escHtml(_appsSpecVersionLabel(data.from_version))}; the version written down for this app is ${escHtml(_appsSpecVersionLabel(data.to_version))}.
    </div>
    ${conflicts.length ? `
      <div style="font-size:0.8rem;color:var(--text);background:var(--bg3);border:1px solid var(--border);border-left:3px solid var(--yellow);border-radius:6px;padding:10px 12px;margin-top:14px">
        <div style="font-weight:600;margin-bottom:4px">${conflicts.length} file${conflicts.length === 1 ? ' was' : 's were'} changed on this bot</div>
        <div style="color:var(--text2);line-height:1.5">An update brings the app's own files up to date. These ones are not what was installed, so updating would discard whatever was changed here. Nothing is touched unless you say so below.</div>
      </div>` : ''}
    <div style="font-size:0.75rem;color:var(--text2);margin:14px 0 4px">${moving.length} file${moving.length === 1 ? '' : 's'} would change</div>
    <div class="resp-table-wrap" style="max-height:240px;overflow-y:auto">
      <table class="resp-table">
        <thead><tr><th>File</th><th>State</th></tr></thead>
        <tbody>${items.map(i => `
          <tr>
            <td data-label="File"><code style="font-size:0.72rem">${escHtml(i.rel)}</code></td>
            <td data-label="State">${_appsUpdateStateCell(i)}</td>
          </tr>`).join('')}</tbody>
      </table>
    </div>
    ${basis}
    ${dropped.length ? `
      <div style="font-size:0.75rem;color:var(--text3);margin-top:10px">
        This version no longer includes ${dropped.map(p => `<code style="font-size:0.72rem">${escHtml(p)}</code>`).join(' ')}. ${dropped.length === 1 ? 'It is' : 'They are'} left on the bot — removing a bot's files is a separate decision, not something an update does quietly.
      </div>` : ''}
    ${conflicts.length ? `
      <label style="display:flex;align-items:flex-start;gap:8px;font-size:0.78rem;color:var(--text2);margin-top:14px;cursor:pointer">
        <input type="checkbox" id="app-update-confirm" onchange="_appsUpdateConfirm(this.checked)" style="margin-top:2px">
        <span>Replace the ${conflicts.length} changed file${conflicts.length === 1 ? '' : 's'} too, discarding what was changed on this bot.</span>
      </label>` : ''}
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px;padding-top:12px;border-top:1px solid var(--border)">
      <button class="btn btn-ghost" onclick="appsCloseInstall()">Cancel</button>
      <button class="btn btn-primary" id="app-update-apply" ${conflicts.length ? 'disabled' : ''}
        onclick="appsApplyUpdate()">Update to ${escHtml(_appsSpecVersionLabel(data.to_version))}</button>
    </div>`;
}

function _appsUpdateConfirm(checked) {
  if (_appsInstall) _appsInstall.confirm = !!checked;
  const btn = document.getElementById('app-update-apply');
  if (btn) btn.disabled = !checked;
}

async function appsApplyUpdate() {
  const state = _appsInstall;
  if (!state || !state.preview) return;
  const conflicts = (state.preview.conflicts || []).length;
  if (conflicts && !state.confirm) return;
  _appsInstallBusy(`Updating ${_appsBotName(state.botId)}…`);
  let data;
  try {
    data = await api('POST', `/api/apps/${encodeURIComponent(state.appId)}/update`, {
      bot_id: state.botId, dry_run: false, confirm_overwrite: !!state.confirm,
    });
  } catch (err) {
    _appsInstallError('update this app', err);
    return;
  }
  if (!data || data.ok !== true) { _appsRenderInstallResult(data, false); return; }
  const body = _appsInstallBody();
  const n = (data.applied || []).length;
  toast(`✓ Updated ${_appsBotName(data.bot_id)}`, 'ok');
  if (body) {
    body.innerHTML = `
      <div style="font-size:0.85rem;margin-bottom:8px">${escHtml(_appsBotName(data.bot_id))} is now on <b>${escHtml(_appsSpecVersionLabel(data.to_version))}</b> — ${n} file${n === 1 ? '' : 's'} changed.</div>
      <div style="font-size:0.78rem;color:var(--text2);line-height:1.5">
        Each file was checked after it was written, against the version this bot was actually on when the change was worked out.
      </div>
      <div style="display:flex;justify-content:flex-end;margin-top:16px">
        <button class="btn btn-primary" onclick="appsCloseInstall();appsShowDetail('${escHtml(data.app_id)}')">Done</button>
      </div>`;
  }
}

window.appsOpenInstall = appsOpenInstall;
window.appsOpenUpdate = appsOpenUpdate;
window.appsCloseInstall = appsCloseInstall;
window.appsApplyInstall = appsApplyInstall;
window.appsApplyUpdate = appsApplyUpdate;
window._appsInstallPickTarget = _appsInstallPickTarget;
window._appsInstallPickSource = _appsInstallPickSource;
window._appsUpdateConfirm = _appsUpdateConfirm;

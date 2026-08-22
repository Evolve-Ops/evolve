// ════════════════════════════════════════════════════════════════════════
// Page: Apps → Discovered (pod-wide drafts) — AL-1.8a
//
// The scanner finds things on a bot that LOOK like apps. Until somebody
// vouches for one it is a draft: no identity, no Tier-1 menu line, and the
// scanner is still free to merge, rename or drop it. This tab is where the
// admin sees every draft on the pod at once and decides.
//
// THE TWO COLUMNS 1.8a LEFT EMPTY ARE FILLED (AL-1.8b, design §5/§6):
//
//   Readiness — AL-1.6b's score. Null still renders "not yet scored": a pod
//               whose scorer cannot be reached must not show a zero, and
//               "scored 0" and "nothing scored this" are different facts.
//   Offer     — AL-1.7's promotion offer, read from the manifest's own
//               "never"/snooze answers and from the Proposal the offer
//               becomes. A FIFTH state, "couldn't check", exists for the
//               case where the proposal store cannot be read — reporting
//               that as "not yet offered" would be a guess wearing a fact's
//               clothes.
//
// CLICKING A ROW OPENS A DRAWER (D-U7), not a page. The queue is a
// reviewing flow: the admin is going down a list deciding, and a full
// navigation per draft would cost them their place. The drawer carries the
// concrete evidence — the files, the schedule, the standing-instruction
// sections, the recurring ask with who asked and on how many days — and the
// SAME two actions the row has. No new action appears only in the drawer.
//
// Sync / rescan lives here rather than on the front page: discovery is a
// background process and the operator wants its RESULTS, not its button.
// ════════════════════════════════════════════════════════════════════════

let _appsDiscoveredData = null;
let _appsDiscoveredFilterBot = '';
let _appsDiscoveredDraftData = null;

async function appsLoadDiscovered() {
  const host = document.getElementById('apps-discovered-body');
  if (!host) return;
  host.innerHTML = `<div class="summary-band-loading"><div class="spinner" style="width:12px;height:12px;border-width:1.5px"></div> Looking for drafts across the pod…</div>`;
  let data;
  try {
    data = await api('GET', '/api/apps/discovered');
  } catch (err) {
    host.innerHTML = _appsErrorBox('the pod\'s drafts', err);
    return;
  }
  if (!data || data.ok !== true) {
    host.innerHTML = _appsErrorBox('the pod\'s drafts', data);
    return;
  }
  _appsDiscoveredData = data;
  _appsRenderDraftBotFilter();
  _appsRenderDiscovered();
}

// The same control the Apps subtab has (operator, 2026-08-21 — the shell
// shipped it on Apps only). Options are every bot on the pod, not just the
// ones that happen to have a draft today: a picker that hides a bot cannot
// be used to ask whether that bot has anything.
function _appsRenderDraftBotFilter() {
  const sel = document.getElementById('apps-discovered-filter-bot');
  if (!sel || !_appsDiscoveredData) return;
  const bots = _appsDiscoveredData.bots || [];
  sel.innerHTML = `<option value="">All bots</option>` + bots.map(b =>
    `<option value="${escHtml(b)}"${b === _appsDiscoveredFilterBot ? ' selected' : ''}>${escHtml(
      typeof botLabel === 'function' ? botLabel(b) : b)}</option>`
  ).join('');
}

function appsDiscoveredApplyFilters() {
  _appsDiscoveredFilterBot = document.getElementById('apps-discovered-filter-bot')?.value || '';
  _appsRenderDiscovered();
}

// What kind of evidence the scanner actually has. Derived from what exists
// on the manifest, so an empty list means "nothing recorded" rather than a
// missing label.
function _appsEvidenceChips(kinds) {
  const meta = {
    files: ['Files', 'Files in the bot\'s workspace belong to this draft.'],
    cron: ['Schedule', 'Something runs on a schedule for this draft.'],
    memory: ['Standing instructions', 'The bot\'s own notes describe this behavior.'],
    conversation: ['Keeps being asked for', 'Someone keeps asking the bot for this. Nothing on disk backs it yet — the asking IS the evidence.'],
  };
  if (!kinds || !kinds.length) {
    return `<span style="color:var(--text3)" title="The scanner recorded no files, schedule or standing instruction for this draft.">nothing recorded</span>`;
  }
  return kinds.map(k => {
    const m = meta[k] || [k, ''];
    return `<span class="badge badge-sm badge-neutral" title="${escHtml(m[1])}">${escHtml(m[0])}</span>`;
  }).join(' ');
}

function _appsRenderDiscovered() {
  const host = document.getElementById('apps-discovered-body');
  const all = (_appsDiscoveredData && _appsDiscoveredData.drafts) || [];
  const drafts = _appsDiscoveredFilterBot
    ? all.filter(d => d.bot_id === _appsDiscoveredFilterBot)
    : all;
  if (!host) return;

  if (!all.length) {
    host.innerHTML = _appsEmpty(
      'No drafts waiting.',
      'Everything the scanner has found on this pod has been vouched for or set aside. Run Sync to look again.',
    );
    return;
  }
  if (!drafts.length) {
    host.innerHTML = _appsEmpty(
      'No drafts on this bot.',
      `${all.length} draft${all.length === 1 ? '' : 's'} elsewhere on the pod — choose All bots to see them.`,
    );
    return;
  }

  host.innerHTML = `
    <div class="card" style="padding:0;overflow:hidden">
      <div class="resp-table-wrap">
        <table class="resp-table">
          <thead>
            <tr>
              <th>What it looks like</th>
              <th>Bot</th>
              <th>Evidence</th>
              <th>Readiness</th>
              <th>Offer</th>
              <th></th>
            </tr>
          </thead>
          <tbody>${drafts.map(_appsDiscoveredRow).join('')}</tbody>
        </table>
      </div>
    </div>
    <div style="font-size:0.75rem;color:var(--text3);margin-top:10px">
      Showing ${drafts.length} of ${all.length} draft${all.length === 1 ? '' : 's'} across the pod.
      Promoting one vouches for it: the scanner keeps its details current but stops merging,
      renaming or removing it on its own.
      ${_appsDiscoveredData && _appsDiscoveredData.offers_readable === false
        ? '<div style="margin-top:6px">The pod could not check what has been offered, so the Offer column says so rather than reporting "not yet offered".</div>'
        : ''}
    </div>`;
}

function _appsDiscoveredRow(d) {
  const label = typeof botLabel === 'function' ? botLabel(d.bot_id) : d.bot_id;
  const nameArg = (d.name || d.manifest_stem).replace(/'/g, "\\'");
  const ref = d.draft_id || d.manifest_stem;
  const open = `appsOpenDiscoveredDraft('${escHtml(d.bot_id)}','${escHtml(ref)}')`;
  return `
    <tr class="apps-row" role="button" tabindex="0" style="cursor:pointer"
        onclick="${open}"
        onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();${open}}">
      <td data-label="What it looks like">
        <div style="font-weight:600">${escHtml(d.name || d.manifest_stem)}</div>
        ${d.purpose
          ? `<div style="font-size:0.78rem;color:var(--text2);max-width:420px;overflow:hidden;text-overflow:ellipsis">${escHtml(d.purpose)}</div>`
          : `<div style="font-size:0.78rem;color:var(--text3)">The scanner has not worked out what this does yet</div>`}
      </td>
      <td data-label="Bot">${escHtml(label)}</td>
      <td data-label="Evidence">${_appsEvidenceChips(d.evidence)}</td>
      <td data-label="Readiness">${_appsReadinessCell(d.readiness)}</td>
      <td data-label="Offer">${_appsOfferCell(d.offer)}</td>
      <td data-label="">
        <div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end"
             onclick="event.stopPropagation()">
          ${_appsDraftActions(d.bot_id, d.manifest_stem, nameArg)}
        </div>
      </td>
    </tr>`;
}

// The two actions, in one place, so the row and the drawer cannot drift
// apart — D-U7 says the drawer offers "the same actions as the row, no new
// ones", and the cheapest way to keep that true is to have one function.
function _appsDraftActions(botId, stem, nameArg) {
  return `
    <button class="btn btn-ghost btn-sm"
      onclick="promoteDefinition('${escHtml(botId)}','${escHtml(stem)}','${escHtml(nameArg)}')"
      title="Vouch for this as a real app, on the user's behalf.">Promote</button>
    <button class="btn btn-ghost btn-sm"
      onclick="appsNeverOfferDraft('${escHtml(botId)}','${escHtml(stem)}','${escHtml(nameArg)}')"
      title="Archive this draft so it drops out of the list.">Never</button>`;
}

// Readiness. Null is "nothing scored this", which is not a zero — and a
// score computed from one measured dimension out of three says so, because
// a composite standing on one leg is not the three-part judgement its name
// suggests (app_readiness's own finding).
function _appsReadinessCell(readiness) {
  if (!readiness || readiness.score == null) {
    return `<span style="color:var(--text3)" title="Nothing on this pod has scored this draft. That is 'not scored', not 'scored low'.">not yet scored</span>`;
  }
  const band = {
    ready: ['badge-ok', 'Ready to put to its user.'],
    emerging: ['badge-neutral', 'Some signal, not enough to ask about yet.'],
    weak: ['badge-neutral', 'Little sign this is a real recurring app.'],
    unscored: ['badge-neutral', 'Nothing measurable.'],
  }[readiness.band] || ['badge-neutral', ''];
  const partial = readiness.dimensions_measured < readiness.dimensions_total
    ? `<div style="font-size:0.72rem;color:var(--text3)">from ${readiness.dimensions_measured} of ${readiness.dimensions_total} measures</div>`
    : '';
  return `<span class="badge badge-sm ${band[0]}" title="${escHtml(band[1])}">${readiness.score}</span>${partial}`;
}

function _appsOfferCell(offer) {
  const o = offer || {};
  if (o.state === 'never') {
    return `<span class="badge badge-sm badge-neutral" title="${escHtml(
      'The user said never. The bot will not raise this again'
      + (o.by ? ` (recorded by ${o.by})` : '') + '.')}">never</span>`;
  }
  if (o.state === 'snoozed') {
    // A snooze expiry is in the FUTURE, and ago() renders anything future as
    // "just now" — so this is an absolute pod-local time, not a relative one.
    const until = o.until && typeof fmtPodTime === 'function'
      ? fmtPodTime(o.until) : (o.until || '');
    return `<span class="badge badge-sm badge-neutral" title="The user deferred this. The bot stays quiet until then.">quiet until ${escHtml(until || 'later')}</span>`;
  }
  if (o.state === 'offered') {
    const when = o.at ? ` ${escHtml(ago(o.at))}` : '';
    if (o.declined) {
      return `<span class="badge badge-sm badge-neutral" title="The bot asked and the answer was no.">declined${when}</span>`;
    }
    return `<span class="badge badge-sm badge-neutral" title="${escHtml(
      'The bot has put this to its user' + (o.to ? ` (${o.to.replace(/_/g, ' ')})` : '')
      + '. Waiting on an answer.')}">offered${when}</span>`;
  }
  if (o.state === 'unknown') {
    return `<span style="color:var(--text3)" title="The record of what has been offered could not be read, so this is 'we could not check' rather than 'nobody has asked'.">couldn't check</span>`;
  }
  return `<span style="color:var(--text3)" title="No bot has put this to its user yet.">not yet offered</span>`;
}

// "Never" archives the draft through the existing archive endpoint. It is
// NOT the permanent shield the promotion flow will write (AL-1.7's
// do_not_offer) — this chip adds no write route — so the confirmation says
// exactly what it does and does not do, rather than implying permanence the
// pod cannot yet deliver.
async function appsNeverOfferDraft(botId, stem, name) {
  const nm = name || stem;
  if (!await confirmModal({
    title: 'Set this draft aside?',
    body: `Archive "${nm}" so it stops appearing here.\n\nThe files stay exactly where they are — this only hides the draft. If the scanner keeps finding the same thing it can come back; a permanent "never offer this" record arrives with the promotion offer work.`,
    confirmLabel: 'Set aside',
  })) return;
  const r = await api('POST', `/api/applications/${encodeURIComponent(botId)}/${encodeURIComponent(stem)}/archive`, {});
  if (r && r.ok !== false && !r.error) {
    toast('✓ Draft set aside', 'ok');
    // Close the drawer first: it is showing a draft that is about to stop
    // being in the list, and leaving it open would offer actions on it.
    appsCloseDiscoveredDraft();
    await appsLoadDiscovered();
  } else {
    toast('✗ ' + ((r && r.error) || 'Could not archive the draft'), 'err');
  }
}



// ── The drawer (design §4a / D-U7) ─────────────────────────────────────────
//
// NAMED "…DiscoveredDraft", not "…Draft": index.html's Create-wizard already
// defines a global ``appsOpenDraft(sessionId)`` for its "Recent drafts" rows,
// and these are classic scripts sharing one global scope — the later
// definition silently wins. A shorter name here would have opened the
// authoring wizard from the queue instead of this drawer.

async function appsOpenDiscoveredDraft(botId, ref) {
  const drawer = document.getElementById('apps-draft-drawer');
  const body = document.getElementById('apps-draft-drawer-body');
  if (!drawer || !body) return;
  drawer.classList.add('open');
  drawer.setAttribute('aria-hidden', 'false');
  document.getElementById('apps-draft-drawer-title').textContent = 'Loading…';
  body.innerHTML = `<div class="summary-band-loading"><div class="spinner" style="width:12px;height:12px;border-width:1.5px"></div> Reading what the scanner found…</div>`;
  let data;
  try {
    data = await api('GET', `/api/apps/discovered/${encodeURIComponent(ref)}?bot=${encodeURIComponent(botId)}`);
  } catch (err) {
    body.innerHTML = _appsErrorBox('this draft', err);
    return;
  }
  if (!data || data.ok !== true) {
    body.innerHTML = _appsErrorBox('this draft', data);
    return;
  }
  _appsDiscoveredDraftData = data;
  _appsRenderDiscoveredDrawer();
}

// Esc closes it, the same way every modal on this page does. Registered
// once, at module load: the drawer's markup is in the page from the start,
// so there is no "attach on open / detach on close" bookkeeping to get
// wrong. Non-modal by design — the list behind it stays usable.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  // A modal on top of the drawer owns the key first. confirmModal's own
  // handler runs in the capture phase and does not stop propagation, so
  // without this check one Esc would cancel the confirmation AND close the
  // drawer behind it — losing the operator's place for a keystroke they
  // meant as "no, not that".
  if (document.querySelector('.modal-overlay.open')) return;
  const drawer = document.getElementById('apps-draft-drawer');
  if (drawer && drawer.classList.contains('open')) appsCloseDiscoveredDraft();
});

function appsCloseDiscoveredDraft() {
  const drawer = document.getElementById('apps-draft-drawer');
  if (!drawer) return;
  drawer.classList.remove('open');
  drawer.setAttribute('aria-hidden', 'true');
  _appsDiscoveredDraftData = null;
}

function _appsDraftSection(title, inner) {
  if (!inner) return '';
  return `
    <div style="margin-top:16px">
      <div class="card-title" style="margin-bottom:6px">${escHtml(title)}</div>
      ${inner}
    </div>`;
}

function _appsDraftList(items, total) {
  if (!items || !items.length) return '';
  const more = (total && total > items.length)
    ? `<li style="color:var(--text3);list-style:none">…and ${total - items.length} more</li>`
    : '';
  return `<ul style="margin:0;padding-left:18px;font-size:0.78rem;line-height:1.6">${
    items.map(i => `<li>${i}</li>`).join('')}${more}</ul>`;
}

// The recurring-ask evidence, in a sentence. This is the only evidence a
// conversation-only draft has, so it has to read as a reason rather than as
// a row of numbers.
function _appsDraftConversation(c) {
  if (!c || !Object.keys(c).length) return '';
  const bits = [];
  if (c.label) bits.push(`Asked for: “${escHtml(c.label)}”`);
  if (c.days_seen != null && c.window_days != null) {
    bits.push(`on ${c.days_seen} of the last ${c.window_days} days`);
  }
  if (c.occurrences != null) bits.push(`${c.occurrences} times in all`);
  if (c.center_hour != null) {
    bits.push(`usually around ${String(c.center_hour).padStart(2, '0')}:00`);
  }
  const who = c.primary_requester
    ? `<div style="margin-top:4px">Asked by ${escHtml(c.primary_requester)}${
        (c.requesters && c.requesters.length > 1)
          ? ` and ${c.requesters.length - 1} other${c.requesters.length === 2 ? '' : 's'}` : ''}.</div>`
    : '';
  const span = (c.first_day && c.last_day)
    ? `<div style="margin-top:4px;color:var(--text3)">${escHtml(c.first_day)} to ${escHtml(c.last_day)}</div>`
    : '';
  return `<div style="font-size:0.78rem;line-height:1.6">${bits.join(', ')}.${who}${span}</div>`;
}

function _appsRenderDiscoveredDrawer() {
  const d = _appsDiscoveredDraftData;
  const body = document.getElementById('apps-draft-drawer-body');
  const foot = document.getElementById('apps-draft-drawer-actions');
  if (!d || !body) return;
  const label = typeof botLabel === 'function' ? botLabel(d.bot_id) : d.bot_id;
  document.getElementById('apps-draft-drawer-title').textContent = d.name || d.manifest_stem;
  const ev = d.evidence || {};

  const files = _appsDraftList(
    (ev.files || []).map(f => `<code style="font-size:0.72rem">${escHtml(f)}</code>`),
    ev.files_total);
  const schedules = _appsDraftList((ev.schedules || []).map(sc => {
    const when = sc.when ? `<b>${escHtml(sc.when)}</b>` : '<span style="color:var(--text3)">timing not recorded</span>';
    const what = sc.what ? ` — ${escHtml(sc.what)}` : '';
    const where = sc.where ? ` <span style="color:var(--text3)">(${escHtml(sc.where)})</span>` : '';
    return `${when}${what}${where}`;
  }), ev.schedules_total);
  const memory = (ev.memory && (ev.memory.path || (ev.memory.sections || []).length))
    ? `<div style="font-size:0.78rem;line-height:1.6">${
        ev.memory.path ? `In <code style="font-size:0.72rem">${escHtml(ev.memory.path)}</code>` : 'In the bot\'s standing instructions'}${
        (ev.memory.sections || []).length ? `, under ${ev.memory.sections.map(x => escHtml(x)).join(', ')}` : ''}.</div>`
    : '';
  const conversation = _appsDraftConversation(ev.conversation);
  const nothing = (!files && !schedules && !memory && !conversation)
    ? `<div style="font-size:0.78rem;color:var(--text3)">The scanner recorded no files, schedule, standing instruction or recurring request for this draft.</div>`
    : '';

  body.innerHTML = `
    <div style="font-size:0.75rem;color:var(--text3)">On ${escHtml(label)}${
      d.created_at ? ` · first seen ${escHtml(ago(d.created_at))}` : ''}</div>
    <div style="font-size:0.85rem;color:var(--text2);margin-top:8px;line-height:1.6">${
      d.description || d.purpose
        ? escHtml(d.description || d.purpose)
        : '<span style="color:var(--text3)">The scanner has not worked out what this does yet.</span>'}</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;align-items:center">
      ${_appsEvidenceChips(ev.kinds)}
    </div>

    ${_appsDraftSection('Why it thinks this', nothing || '')}
    ${_appsDraftSection('Files', files)}
    ${_appsDraftSection('Schedule', schedules)}
    ${_appsDraftSection('Standing instructions', memory)}
    ${_appsDraftSection('Recurring request', conversation)}

    <div style="margin-top:18px;display:flex;gap:18px;flex-wrap:wrap">
      <div>
        <div class="card-title" style="margin-bottom:4px">Readiness</div>
        ${_appsReadinessCell(d.readiness)}
      </div>
      <div>
        <div class="card-title" style="margin-bottom:4px">Offer</div>
        ${_appsOfferCell(d.offer)}
      </div>
    </div>`;

  if (foot) {
    const nameArg = (d.name || d.manifest_stem).replace(/'/g, "\\'");
    // Deliberately the SAME two buttons the row carries (D-U7): a drawer
    // that grew an action of its own would be a second place to decide.
    foot.innerHTML = `<div style="display:flex;gap:8px;justify-content:flex-end">${
      _appsDraftActions(d.bot_id, d.manifest_stem, nameArg)}</div>`;
  }
}


window.appsLoadDiscovered = appsLoadDiscovered;
window.appsNeverOfferDraft = appsNeverOfferDraft;
window.appsDiscoveredApplyFilters = appsDiscoveredApplyFilters;
window.appsOpenDiscoveredDraft = appsOpenDiscoveredDraft;
window.appsCloseDiscoveredDraft = appsCloseDiscoveredDraft;

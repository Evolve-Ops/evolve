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
//
// EVERY EMPTY STATE BRANCHES ON WHAT THE SCAN DID (ALPHA-2; audit findings
// B2a, U2, U5). /api/apps/discovered carries a per-bot scan-provenance block
// with four states — never scanned / ok / degraded / could not read — and this
// page must never assert past it:
//
//   - An empty list on a pod nobody has scanned says SO. The old single empty
//     branch said "everything the scanner has found has been vouched for or
//     set aside", which on a first open is false in the most discouraging
//     direction available: it tells a new operator the scanner already looked.
//   - A scan that ran without a model gets a banner naming the reason and the
//     fix, above whatever the table shows. Without it, "discovered 0 app(s)"
//     reads as a finding when it is a consequence of a skipped phase.
//   - A "ready" draft that cannot be offered says why on the row. The blocker
//     text comes from the payload — app_readiness.py owns MIN_DIMENSIONS_FOR_
//     OFFER and OFFER_THRESHOLD, and a second copy of either number in here is
//     how a gate ends up enforced on one path and not the other.
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

// ── What the last scan did, in the operator's words (audit B2a / U5) ───────
//
// Every helper below reads the payload's scan-provenance block and NOTHING
// else. When the block is absent (an older server, or a read that could not be
// attempted) they all say "we don't know" rather than assuming a healthy scan —
// which is the whole point: the bug being fixed is a surface asserting past
// what it was told.

function _appsScanSummary() {
  const d = _appsDiscoveredData;
  return (d && d.scan_summary) || null;
}

// Bot ids the caller is looking at right now, so the never-scanned copy under a
// bot filter talks about THAT bot rather than about the pod.
function _appsScansInScope() {
  const d = _appsDiscoveredData;
  const scans = (d && d.scans) || null;
  if (!scans) return null;
  return _appsDiscoveredFilterBot
    ? scans.filter(s => s.bot_id === _appsDiscoveredFilterBot)
    : scans;
}

// The degraded banner. Grouped by reason server-side, so a pod where five bots
// share one missing key produces one line. Renders above whatever the body
// shows — including an empty state, which is exactly the case where "the scan
// found nothing" needs the most explaining.
//
// The headline is deliberately about the LIST, not about the scan: "degraded"
// covers a scan that skipped its model phase AND one with no record of
// finishing (which may be a scan running right now). "The last scan did not run
// in full" would be wrong for the second, and a banner that overstates trains
// operators to stop reading banners.
function _appsScanBanner() {
  const summary = _appsScanSummary();
  if (!summary) return '';
  const reasons = summary.degraded_reasons || [];
  if (!reasons.length) return '';
  const lines = reasons.map(r => {
    const bots = (r.bots || []).map(b =>
      typeof botLabel === 'function' ? botLabel(b) : b);
    const who = bots.length === 1
      ? `On ${bots[0]}: `
      : `On ${bots.length} bots (${bots.join(', ')}): `;
    const remedy = r.remedy ? ` ${r.remedy}` : '';
    return `<div>${escHtml(who)}${escHtml(r.note || 'The last scan did not do everything it normally does.')}${escHtml(remedy)}</div>`;
  }).join('');
  return `<div class="alert alert-warn" style="align-items:flex-start">
    <div>
      <div style="font-weight:600;margin-bottom:4px">Discovered may not be showing everything.</div>
      ${lines}
    </div>
  </div>`;
}

// Bots nobody has scanned, named. Used by both empty branches and by the
// footer, so "personal-bot has never been scanned" is sayable whether or not
// the rest of the pod has drafts.
function _appsBotsInState(state) {
  const scans = _appsScansInScope();
  if (!scans) return [];
  return scans.filter(s => s.state === state).map(s => s.bot_id);
}

function _appsNeverScannedBots() { return _appsBotsInState('never_scanned'); }
function _appsUnreadableScanBots() { return _appsBotsInState('unreadable'); }

// The bots whose last scan actually COMPLETED. This is the denominator for any
// claim of completeness — "everything the scanner found has been dealt with" is
// only true of a bot whose scan finished and skipped nothing.
function _appsCompletedScanBots() { return _appsBotsInState('ok'); }

// The server ships the remedy for each state; the page must not keep a second
// copy of the advice any more than of a threshold. Returns '' when the payload
// carries none, so an older server costs the sentence rather than getting a
// guessed one.
function _appsRemedyFor(botIds) {
  const scans = _appsScansInScope() || [];
  const hit = scans.find(s => botIds.indexOf(s.bot_id) !== -1 && s.remedy);
  return hit ? hit.remedy : '';
}

function _appsBotNames(ids) {
  return ids.map(b => typeof botLabel === 'function' ? botLabel(b) : b).join(', ');
}

// The empty state, chosen by what the pod actually knows rather than assumed.
// Returns [headline, hint].
function _appsDiscoveredEmptyCopy() {
  const scans = _appsScansInScope();
  const scope = _appsDiscoveredFilterBot
    ? (typeof botLabel === 'function' ? botLabel(_appsDiscoveredFilterBot) : _appsDiscoveredFilterBot)
    : 'this pod';
  if (!scans || !scans.length) {
    // No provenance to branch on. Say what is true — there is nothing here —
    // without claiming anything about whether a scan ever ran.
    return ['Nothing here yet.',
      `No drafts are waiting on ${scope}. Choose Sync all bots to look again.`];
  }
  const never = _appsNeverScannedBots();
  const unreadable = _appsUnreadableScanBots();
  if (never.length === scans.length) {
    return ['Nothing has looked here yet.',
      scans.length === 1
        ? `Evolve has never scanned ${scope} for apps. Choose Sync all bots and it will read the bot's workspace, its schedules and its standing instructions, and list anything that looks like an app it could take over.`
        : `Evolve has never scanned ${scope} for apps. Choose Sync all bots and it will read each bot's workspace, its schedules and its standing instructions, and list anything that looks like an app it could take over.`];
  }
  if (unreadable.length === scans.length) {
    const remedy = _appsRemedyFor(unreadable);
    return ['Evolve could not check whether anything has been scanned.',
      `Its record of past scans on ${scope} could not be read, so this is "we could not tell", not "there is nothing here".${remedy ? ' ' + remedy : ''}`];
  }
  // NOTHING COMPLETED. Every bot here was never visited, could not be read, or
  // ran a scan that skipped part of itself — so "everything has been vouched
  // for" is unsupported, and on the audit's own pod (three bots, no provider
  // key, no drafts) the old copy said it anyway, directly contradicting the
  // banner above. The banner carries the reason; this says what it means for
  // the list.
  if (!_appsCompletedScanBots().length) {
    return ['Nothing to show yet.',
      `No scan on ${scope} has finished a full pass, so an empty list here does not mean there is nothing to find. See the note above.`];
  }
  const caveats = [];
  if (never.length) {
    caveats.push(`${_appsBotNames(never)} ${never.length === 1 ? 'has' : 'have'} never been scanned — run Sync to look there`);
  }
  if (unreadable.length) {
    caveats.push(`Evolve could not read the scan record for ${_appsBotNames(unreadable)}`);
  }
  const tail = caveats.length ? ` ${caveats.join('. ')}.` : '';
  return ['No drafts waiting.',
    `Everything the scanner has found on ${scope} has been vouched for or set aside. Run Sync to look again.${tail}`];
}

function _appsRenderDiscovered() {
  const host = document.getElementById('apps-discovered-body');
  const all = (_appsDiscoveredData && _appsDiscoveredData.drafts) || [];
  const drafts = _appsDiscoveredFilterBot
    ? all.filter(d => d.bot_id === _appsDiscoveredFilterBot)
    : all;
  if (!host) return;

  // The banner sits above every branch below, empty states included: a scan
  // that ran without a model is the single most likely reason the list is
  // empty, and hiding the explanation behind "there are rows to show" would
  // withhold it exactly when it is needed.
  const banner = _appsScanBanner();

  if (!all.length) {
    const [headline, hint] = _appsDiscoveredEmptyCopy();
    host.innerHTML = banner + _appsEmpty(headline, hint);
    return;
  }
  if (!drafts.length) {
    const never = _appsNeverScannedBots();
    const hint = never.length
      ? `Evolve has never scanned this bot. Run Sync to look here; ${all.length} draft${all.length === 1 ? '' : 's'} elsewhere on the pod — choose All bots to see them.`
      : `${all.length} draft${all.length === 1 ? '' : 's'} elsewhere on the pod — choose All bots to see them.`;
    host.innerHTML = banner + _appsEmpty(
      never.length ? 'Nothing has looked at this bot yet.' : 'No drafts on this bot.',
      hint,
    );
    return;
  }

  host.innerHTML = banner + `
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
      ${_appsScanCoverageNote()}
    </div>`;
}

// A list of drafts says nothing about the bots that were never looked at, or
// about when anyone last looked. This footer is where both get said, so a pod
// with one scanned bot and two untouched ones does not read as a complete
// queue, and a queue built from a scan five weeks ago does not read as current.
function _appsScanCoverageNote() {
  const never = _appsNeverScannedBots();
  const unreadable = _appsUnreadableScanBots();
  const parts = [];
  if (never.length) {
    parts.push(`${escHtml(_appsBotNames(never))} ${never.length === 1 ? 'has' : 'have'} never been scanned, so nothing here speaks for ${never.length === 1 ? 'it' : 'them'}`);
  }
  if (unreadable.length) {
    parts.push(`Evolve could not read the scan record for ${escHtml(_appsBotNames(unreadable))}`);
  }
  const gaps = parts.length
    ? `<div style="margin-top:6px">${parts.join('. ')}. Run Sync to look again.</div>`
    : '';
  return _appsLastScanLine() + gaps;
}

// "Last scanned <when>". Absent — never a placeholder date — when no bot in
// scope has a recorded scan time, which is the same tri-state discipline the
// rest of this payload keeps.
function _appsLastScanLine() {
  const scans = _appsScansInScope() || [];
  const stamps = scans.map(s => s.last_scan_at).filter(Boolean).sort();
  if (!stamps.length) return '';
  const when = typeof ago === 'function' ? ago(stamps[stamps.length - 1]) : stamps[stamps.length - 1];
  return `<div style="margin-top:6px">Last scanned ${escHtml(when)}.</div>`;
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
//
// A "ready" band beside an offer state of "not yet offered" reads as a broken
// feature unless the row says what is holding it (audit U2 — eight of nine
// drafts on the audit pod looked exactly like that). The blocker text is
// SERVER-SUPPLIED: app_readiness.py owns MIN_DIMENSIONS_FOR_OFFER and
// OFFER_THRESHOLD, and re-deriving either here would give the pod two copies
// of one gate. An older payload with no blocker list simply shows no line —
// the cell under-explains rather than inventing a reason.
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
  return `<span class="badge badge-sm ${band[0]}" title="${escHtml(band[1])}">${readiness.score}</span>${
    _appsReadinessMeta(readiness)}`;
}

// The small grey line under the score: how much of the composite was measured,
// and — when the draft cannot be offered — what is holding it. One line, so a
// "ready" row reads "from 1 of 3 measures · needs another measure" rather than
// stacking two half-explanations.
function _appsReadinessMeta(readiness) {
  const bits = [];
  if (readiness.dimensions_measured < readiness.dimensions_total) {
    bits.push(`from ${readiness.dimensions_measured} of ${readiness.dimensions_total} measures`);
  }
  const blockers = readiness.offer_blockers || [];
  if (blockers.length && blockers[0] && blockers[0].short) bits.push(blockers[0].short);
  if (!bits.length) return '';
  const why = blockers.map(b => b.text).filter(Boolean).join(' ');
  return `<div style="font-size:0.72rem;color:var(--text3)"${
    why ? ` title="${escHtml(why)}"` : ''}>${escHtml(bits.join(' · '))}</div>`;
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

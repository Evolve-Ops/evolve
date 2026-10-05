// ════════════════════════════════════════════════════════════════════════
// Page: Apps → Activity — AL-1.8a
//
// One feed for everything that HAPPENED to an app on this pod: builds and
// installs (the forge), promotions and demotions, publishes to the gallery.
// Newest first, each with its outcome.
//
// This replaces Forge Jobs as a top-level tab (design §3.4). Forge runs are
// not the centre of the page any more — they are one kind of entry, labelled
// "authoring" or "install" — but the in-flight table and its approval panel
// move here UNCHANGED, below the feed, because approving a build is real
// work an operator still has to do and 1.8a moves rather than rewrites what
// survives. pages/forge.js keeps owning it.
//
// The feed is a BOUNDED read: the API caps each source and says whether it
// truncated, and this renderer surfaces that rather than letting a window
// read as "that's everything that happened".
// ════════════════════════════════════════════════════════════════════════

let _appsActivityData = null;

async function appsLoadActivity() {
  // The forge table is a sibling section with its own loader; kick it off
  // first so both halves of the tab fill in together.
  if (typeof loadForgeJobs === 'function') loadForgeJobs();

  const host = document.getElementById('apps-activity-body');
  if (!host) return;
  host.innerHTML = `<div class="summary-band-loading"><div class="spinner" style="width:12px;height:12px;border-width:1.5px"></div> Loading recent activity…</div>`;
  let data;
  try {
    data = await api('GET', '/api/apps/activity');
  } catch (err) {
    host.innerHTML = _appsErrorBox('recent activity', err);
    return;
  }
  if (!data || data.ok !== true) {
    host.innerHTML = _appsErrorBox('recent activity', data);
    return;
  }
  _appsActivityData = data;
  _appsRenderActivity();
}

const _APPS_ACTIVITY_KINDS = {
  install:     ['Installed', 'A gallery package was installed on a bot.'],
  authoring:   ['Built', 'The forge built or rebuilt an app for a bot.'],
  improvement: ['Improved', 'The forge re-ran against an existing app.'],
  update:      ['Updated', 'The forge applied a newer version of a package.'],
  hotfix:      ['Hotfixed', 'The forge applied an out-of-band fix.'],
  promoted:    ['Vouched for', 'Someone promoted a draft to a real app.'],
  demoted:     ['Un-vouched', 'Someone demoted an app back to a draft.'],
  published:   ['Published', 'A version of this app was written to the pod gallery.'],
};

// Outcome wording, plain and non-committal where the record is. A forge job
// that shipped files but failed to install its schedule is NOT a success and
// must not read as one (the API keeps that distinction; so does this).
function _appsOutcomeBadge(outcome) {
  const o = String(outcome || '').toLowerCase();
  if (o === 'complete') return `<span class="badge badge-sm badge-ok">Finished</span>`;
  if (o === 'completed with problems') {
    return `<span class="badge badge-sm badge-warn" title="The build finished and the files are real, but part of it — usually a schedule — did not install.">Finished with problems</span>`;
  }
  if (o === 'failed') return `<span class="badge badge-sm badge-crit">Failed</span>`;
  if (o === 'rejected') return `<span class="badge badge-sm badge-neutral">Rejected</span>`;
  if (o === 'running' || o === 'queued') {
    return `<span class="badge badge-sm badge-neutral">${o === 'running' ? 'Running' : 'Queued'}</span>`;
  }
  if (o === 'awaiting_approval') return `<span class="badge badge-sm badge-warn">Waiting for you</span>`;
  if (o === 'promoted' || o === 'published') return `<span class="badge badge-sm badge-ok">Done</span>`;
  if (o === 'demoted') return `<span class="badge badge-sm badge-neutral">Done</span>`;
  return `<span class="badge badge-sm badge-neutral">${escHtml(outcome || 'unknown')}</span>`;
}

function _appsRenderActivity() {
  const host = document.getElementById('apps-activity-body');
  const d = _appsActivityData;
  if (!host || !d) return;
  const entries = d.entries || [];

  if (!entries.length) {
    host.innerHTML = _appsEmpty(
      'Nothing has happened to an app recently.',
      'Builds, installs, promotions and publishes appear here as they happen.',
    );
    return;
  }

  const truncation = d.truncated
    ? `<div style="font-size:0.75rem;color:var(--text3);margin-top:10px">
         Showing the ${entries.length} most recent of ${escHtml(String(d.total))} recorded events.
       </div>`
    : '';

  host.innerHTML = `
    <div class="card" style="padding:0;overflow:hidden">
      <div class="resp-table-wrap">
        <table class="resp-table">
          <thead>
            <tr><th>When</th><th>What</th><th>App</th><th>Bot</th><th>Outcome</th></tr>
          </thead>
          <tbody>${entries.map(_appsActivityRow).join('')}</tbody>
        </table>
      </div>
    </div>${truncation}`;
}

function _appsActivityRow(e) {
  const meta = _APPS_ACTIVITY_KINDS[e.kind] || [e.kind, ''];
  const bot = e.bot_id
    ? escHtml(typeof botLabel === 'function' ? botLabel(e.bot_id) : e.bot_id)
    : '<span style="color:var(--text3)">pod</span>';
  const appCell = e.app_id
    ? `<span class="link" role="button" tabindex="0"
         onclick="appsOpenFromActivity('${escHtml(e.app_id)}')"
         onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();appsOpenFromActivity('${escHtml(e.app_id)}')}"
       >${escHtml(e.app_id)}</span>`
    : '<span style="color:var(--text3)">—</span>';
  return `
    <tr>
      <td data-label="When"><span title="${escHtml(e.ts || '')}">${escHtml(ago(e.ts))}</span></td>
      <td data-label="What">
        <span title="${escHtml(meta[1])}">${escHtml(meta[0])}</span>
        ${e.detail ? `<div style="font-size:0.75rem;color:var(--text3);max-width:320px;overflow:hidden;text-overflow:ellipsis">${escHtml(e.detail)}</div>` : ''}
      </td>
      <td data-label="App">${appCell}</td>
      <td data-label="Bot">${bot}</td>
      <td data-label="Outcome">${_appsOutcomeBadge(e.outcome)}</td>
    </tr>`;
}

// Jump from a feed row to the app itself. The app may be a draft or already
// gone (an entry records what happened, not what still exists), so
// appsShowDetail's own not-found path is what handles the miss.
function appsOpenFromActivity(appId) {
  const tab = document.querySelector('#page-apps .subtab[data-subtab="apps"]');
  if (tab) tab.click();
  appsShowDetail(appId);
}

window.appsLoadActivity = appsLoadActivity;
window.appsOpenFromActivity = appsOpenFromActivity;

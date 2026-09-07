// ════════════════════════════════════════════════════════════════════════
// Page: Help → "Browse topics" (in-app docs/help corpus viewer)
//
// Renders the public help corpus inside the SPA so operators can READ the
// topics without leaving the pod — works offline / loopback-only: the server
// renders the bundled local markdown (routes_help_docs.py); nothing iframes
// or links out to the external GitHub Pages site.
//
// Data:
//   GET /api/help/topics        → section-grouped topic index (matches Pages)
//   GET /api/help/topic/<slug>  → rendered HTML + metadata for one topic
//
// Entry: helpTopicsActivate() — fired by the "Browse topics" subtab. Loads
// the index once (lazily), renders the section-grouped nav, and auto-opens
// the first topic so the reading pane isn't blank. helpOpenTopic(slug) loads
// and renders a topic; helpTopicsFilter(q) filters the nav list. New topics
// (and SECTIONS edits) appear automatically — nothing here is hardcoded.
// ════════════════════════════════════════════════════════════════════════

let _helpTopicsLoaded = false;   // index fetched once per page-load session
let _helpTopicsData = null;      // cached { sections: [...] }
let _helpCurrentSlug = null;     // topic shown in the reading pane

async function helpTopicsActivate() {
  if (_helpTopicsLoaded) return;
  _helpTopicsLoaded = true;
  const listEl = document.getElementById('help-topics-list');
  if (!listEl) return;
  let data;
  try {
    data = await api('GET', '/api/help/topics');
  } catch (e) {
    _helpTopicsLoaded = false;  // let a later activation retry
    listEl.innerHTML = `<div class="alert alert-error">Couldn't load help topics: ${escHtml(String(e))}</div>`;
    return;
  }
  _helpTopicsData = data || { sections: [] };
  _helpRenderTopicList(_helpTopicsData.sections || []);
  const first = _helpFirstSlug(_helpTopicsData.sections || []);
  if (first && !_helpCurrentSlug) helpOpenTopic(first);
}

function _helpFirstSlug(sections) {
  for (const s of sections) {
    if (s.topics && s.topics.length) return s.topics[0].slug;
  }
  return null;
}

function _helpRenderTopicList(sections) {
  const listEl = document.getElementById('help-topics-list');
  if (!listEl) return;
  if (!sections.length) {
    listEl.innerHTML = '<div class="empty">No help topics found.</div>';
    return;
  }
  listEl.innerHTML = sections.map(sec => {
    const items = (sec.topics || []).map(t => {
      const active = t.slug === _helpCurrentSlug ? ' active' : '';
      // Searchable haystack for the client-side filter (title + concepts).
      const hay = `${t.title} ${(t.concepts || []).join(' ')}`.toLowerCase();
      // Read the slug from the element's dataset at click time rather than
      // interpolating it into the onclick string — escHtml doesn't escape
      // single quotes, so a slug bearing one could otherwise break out of the
      // JS string literal. (The corpus is trusted in-repo content, but this
      // keeps the click path injection-proof regardless.)
      return `<button type="button" class="help-topic-link${active}"
        data-help-slug="${escHtml(t.slug)}" data-haystack="${escHtml(hay)}"
        title="${escHtml(t.summary || '')}"
        onclick="helpOpenTopic(this.dataset.helpSlug)">${escHtml(t.title)}</button>`;
    }).join('');
    return `<div class="help-topic-section">
      <h2 class="help-topic-section-label">${escHtml(sec.title)}</h2>${items}</div>`;
  }).join('');
}

async function helpOpenTopic(slug) {
  const pane = document.getElementById('help-reading-pane');
  if (!pane) return;
  _helpCurrentSlug = slug;
  document.querySelectorAll('#help-topics-list .help-topic-link').forEach(b => {
    b.classList.toggle('active', b.dataset.helpSlug === slug);
  });
  pane.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  let data;
  try {
    data = await api('GET', `/api/help/topic/${encodeURIComponent(slug)}`);
  } catch (e) {
    pane.innerHTML = `<div class="alert alert-error">Couldn't load this topic: ${escHtml(String(e))}</div>`;
    return;
  }
  if (!data || data.error) {
    pane.innerHTML = `<div class="alert alert-error">${escHtml((data && data.error) || 'Topic not found')}</div>`;
    return;
  }
  const dep = data.status === 'deprecated'
    ? '<div class="alert alert-warn">This topic is retired — its content has moved. See below for where to look now.</div>'
    : '';
  const chips = (data.concepts || [])
    .map(c => `<span class="help-concept-chip">${escHtml(c)}</span>`).join('');
  const reviewed = data.last_reviewed ? `Last reviewed ${escHtml(data.last_reviewed)}` : '';
  const meta = (reviewed || chips)
    ? `<div class="help-reading-meta">${reviewed}${chips ? `<span class="help-meta-spacer"></span>${chips}` : ''}</div>`
    : '';
  // data.html is server-rendered from the trusted in-repo corpus.
  pane.innerHTML = `${dep}${data.html || ''}${meta}`;
  pane.scrollTop = 0;
  // Rewire in-corpus cross-links (server tags them with data-help-slug) so
  // they open in-app instead of resolving to a dead .html href.
  pane.querySelectorAll('a.help-xref[data-help-slug]').forEach(a => {
    a.addEventListener('click', _helpXrefClick);
  });
}

function _helpXrefClick(ev) {
  ev.preventDefault();
  const slug = ev.currentTarget.dataset.helpSlug;
  if (slug) helpOpenTopic(slug);
}

function helpTopicsFilter(q) {
  const needle = (q || '').trim().toLowerCase();
  document.querySelectorAll('#help-topics-list .help-topic-section').forEach(sec => {
    let any = false;
    sec.querySelectorAll('.help-topic-link').forEach(b => {
      const show = !needle || (b.dataset.haystack || '').includes(needle);
      b.style.display = show ? '' : 'none';
      if (show) any = true;
    });
    sec.style.display = any ? '' : 'none';
  });
}

// Window-exports for the inline onclick / oninput handlers (CLAUDE.md / ESLint).
window.helpTopicsActivate = helpTopicsActivate;
window.helpOpenTopic = helpOpenTopic;
window.helpTopicsFilter = helpTopicsFilter;

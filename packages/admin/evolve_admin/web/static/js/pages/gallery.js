// ════════════════════════════════════════════════════════════════════════
// Page: Gallery (Apps → Gallery subtab)
//
// The App Gallery subtab of the Apps page. Lists all installable apps
// from the curated package catalog with tag-based filtering, import-by-
// URL flow, details modal with preview/preflight, and the install
// wizard (single-bot or multi-bot with cost confirmation).
//
// pageRedirectRegistry maps "gallery" → page: "apps", subtab: "gallery"
// (subtab handler dispatch in onSubTabActivate calls loadGallery).
//
// identity: see applications/app_identity.py::resolve_app_id and its TS twin
// apps/appIdentity.ts::appIdOf (AL-1.4b, internal/build-AL-1.4-app-id-canonical.md
// §3). Every `pkg_id` on this page is a GALLERY CATALOG key, never a manifest
// read: it is the catalog's primary key, the path segment in
// /api/gallery/<pkg_id>, and what an install is dispatched with. Two reasons
// the resolver must not be substituted here:
//
//   * this page renders catalog rows, not manifests. gallery/index.json has
//     carried an `app_id` column since #3413 holding the app SCRIPT name
//     (app_task_manager), which is not the package key; #3681 made the
//     resolver fall THROUGH such values to pkg_id, so it would return the
//     right string today and the script name the day a row gains a
//     conforming app_id.
//   * `display_name || name || pkg_id` below is a LABEL fallback ending at
//     the key, not an identity chain — the alternatives are human strings.
//
// `spec_id` on this page is likewise the published Spec's id in the
// gallery tier path, reported back by the publish endpoint.
//
// State:
//   _galleryData                  — last fetched gallery catalog
//   _galleryInstallPkgId          — package id currently being installed
//   _galleryTagIndex              — { tag: [pkg_id, ...] } reverse index
//   _galleryTagKinds              — { tag: "canonical" | "suite" | "freeform" }
//   _galleryTagFilter             — active tag selection (Set, OR semantics)
//   _galleryDetailsPkgId          — package id in the details modal
//   _galleryFullManifests         — cache of full manifest fetches
//
// Functions (loader + filter + import + install + details flows):
//   loadGallery / renderGallery / renderGalleryTagFilter
//   toggleGalleryTagFilter / clearGalleryTagFilter
//   openGalleryImport / closeGalleryImport / submitGalleryImport
//   openGalleryInstall / closeGalleryInstall / submitGalleryInstall
//     + _renderGalleryCostConfirm (multi-bot cost confirmation modal)
//   openGalleryDetails / closeGalleryDetails / _renderGalleryDetailsBody
//   openGalleryInstallFromDetails (handoff from details modal → install)
//   checkGalleryPreflight / _renderPreflightResults / runInstallPreflight
//
// Cross-file linkages (runtime free-variable lookup):
//   - api(), toast(), escHtml(), botLabel() — core/
//   - _showGalleryPackagePreview (called from apps.js's viewForgeManifest
//     fallback path; resolves at call time)
//   - openProposalDetail (when install creates a proposal) — resolves
//     via self-improvement.js global
// ════════════════════════════════════════════════════════════════════════

// APP GALLERY
// ═══════════════════════════════════════════════════════════════════════════

let _galleryData = [];
let _galleryInstallPkgId = null;

// Gallery tag-filter state. Kinds are loaded once from /api/gallery/tags
// alongside the reverse index; _galleryTagFilter is the active selection
// (Set of tag strings). OR semantics: an app passes if it carries ANY
// of the selected tags.
let _galleryTagIndex = {};   // {tag: [pkg_id, ...]}
let _galleryTagKinds = {};   // {tag: "canonical" | "suite" | "freeform"}
const _galleryTagFilter = new Set();

async function loadGallery() {
  try {
    // Fetch the gallery + the tag index in parallel — the latter feeds
    // the filter chip row, the former feeds the grid.
    const [galleryRes, tagsRes] = await Promise.all([
      api('GET', '/api/gallery'),
      api('GET', '/api/gallery/tags').catch(() => ({tags: {}, kinds: {}})),
    ]);
    _galleryData = galleryRes.packages || [];
    _galleryTagIndex = tagsRes.tags || {};
    _galleryTagKinds = tagsRes.kinds || {};
    renderGalleryTagFilter();
    renderGallery();
  } catch(e) {
    document.getElementById('gallery-grid').innerHTML =
      `<div class="empty" style="color:var(--red)">Failed to load gallery: ${escHtml(e.message)}</div>`;
  }
}

// Render the filter chip row. Order: suite chips first (operator-curated,
// most discoverable), then canonical (auto-detector vocab), then freeform.
// Within each kind, sort by descending pkg count so the most-used tags
// surface first. A "Clear" link appears whenever any chip is selected.
function renderGalleryTagFilter() {
  const el = document.getElementById('gallery-tag-filter');
  if (!el) return;

  const entries = Object.entries(_galleryTagIndex);
  if (!entries.length) { el.innerHTML = ''; return; }

  const kindOrder = {suite: 0, canonical: 1, freeform: 2};
  entries.sort(([a, ap], [b, bp]) => {
    const ka = kindOrder[_galleryTagKinds[a] || 'freeform'] ?? 2;
    const kb = kindOrder[_galleryTagKinds[b] || 'freeform'] ?? 2;
    if (ka !== kb) return ka - kb;
    if (bp.length !== ap.length) return bp.length - ap.length;
    return a.localeCompare(b);
  });

  const chips = entries.map(([tag, pkgs]) => {
    const kind = _galleryTagKinds[tag] || 'freeform';
    const active = _galleryTagFilter.has(tag) ? ' gtag-chip-active' : '';
    return `<span class="gtag-chip gtag-chip-${kind} gtag-chip-btn${active}"
              onclick="toggleGalleryTagFilter('${escHtml(tag)}')"
              title="${escHtml(kind)} tag · ${pkgs.length} app${pkgs.length === 1 ? '' : 's'}">${escHtml(tag)}<span class="gtag-chip-count">${pkgs.length}</span></span>`;
  }).join('');

  const clearLink = _galleryTagFilter.size
    ? `<a href="javascript:void(0)" onclick="clearGalleryTagFilter()" style="font-size:0.72rem;color:var(--text3);margin-left:4px">Clear (${_galleryTagFilter.size})</a>`
    : '';

  el.innerHTML = chips + clearLink;
}

function toggleGalleryTagFilter(tag) {
  if (_galleryTagFilter.has(tag)) _galleryTagFilter.delete(tag);
  else _galleryTagFilter.add(tag);
  renderGalleryTagFilter();
  renderGallery();
}

function clearGalleryTagFilter() {
  _galleryTagFilter.clear();
  renderGalleryTagFilter();
  renderGallery();
}

function renderGallery() {
  const grid = document.getElementById('gallery-grid');
  const q = (document.getElementById('gallery-search')?.value || '').toLowerCase();
  const statusFilter = document.getElementById('gallery-filter-status')?.value || '';

  let pkgs = _galleryData;

  if (q) {
    pkgs = pkgs.filter(p =>
      (p.display_name || '').toLowerCase().includes(q) ||
      (p.name || '').toLowerCase().includes(q) ||
      (p.objective || '').toLowerCase().includes(q) ||
      (p.tags || []).some(t => t.toLowerCase().includes(q))
    );
  }
  if (statusFilter === 'installed') {
    pkgs = pkgs.filter(p => (p.installed_on || []).length > 0);
  } else if (statusFilter === 'not-installed') {
    pkgs = pkgs.filter(p => (p.installed_on || []).length === 0);
  }

  // Tag filter — OR semantics: app passes if it carries ANY active tag.
  if (_galleryTagFilter.size) {
    pkgs = pkgs.filter(p =>
      (p.tags || []).some(t => _galleryTagFilter.has(t))
    );
  }

  if (!pkgs.length) {
    grid.innerHTML = `<div class="empty">No apps found.</div>`;
    return;
  }

  grid.innerHTML = pkgs.map(p => {
    const installed = p.installed_on || [];
    // Per-card tag chips use the same .gtag-chip-* classes as the filter
    // row, plus .gtag-chip-btn so a click toggles that tag into the
    // filter. Mirrors the "show all in this suite" affordance from the
    // task spec — suite chips light up the whole suite when clicked.
    const tags = (p.tags || []).map(t => {
      const kind = _galleryTagKinds[t] || 'freeform';
      const active = _galleryTagFilter.has(t) ? ' gtag-chip-active' : '';
      return `<span class="gtag-chip gtag-chip-${kind} gtag-chip-btn${active}"
                onclick="event.stopPropagation();toggleGalleryTagFilter('${escHtml(t)}')"
                style="margin:0 3px 3px 0"
                title="${escHtml(kind)} tag — click to filter">${escHtml(t)}</span>`;
    }).join('');
    // Per-bot installed chips
    const installedChips = installed.length
      ? `<div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px">`
        + installed.map(b => `<span class="badge badge-ok">${escHtml(botLabel(b))}</span>`).join('')
        + `</div>`
      : '';
    const version = p.pkg_version ? `<span style="font-size:0.7rem;color:var(--text3)">v${escHtml(p.pkg_version)}</span>` : '';
    // F-P.12.b — provenance badge (trust axis). Render the provenance
    // field when available; fall back to deriving it from the legacy
    // ``source`` field so this code works before F-P.12.a merges.
    //
    //   evolve         → no badge (default, ships with Evolve, trusted)
    //   community      → yellow "community" chip + tooltip
    //   operator-local → purple "your app" chip + tooltip
    let provenance = p.provenance || '';
    if (!provenance) {
      provenance = (p.source === 'imported') ? 'community' : 'evolve';
    }
    let source = '';
    if (provenance === 'community') {
      source = `<span class="badge badge-inline" style="margin-left:4px;background:var(--yellow);color:var(--bg1);font-size:0.65rem" title="Community-contributed — imported from an external operator. Review before install.">community</span>`;
    } else if (provenance === 'operator-local') {
      source = `<span class="badge badge-inline" style="margin-left:4px;background:var(--accent);color:var(--bg1);font-size:0.65rem" title="Your app — built or imported by you, not yet shared publicly.">your app</span>`;
    }
    const hasDeps = (p.app_dependencies || []).length > 0;
    const hasReqs = Object.values(p.requirements || {}).some(v => Array.isArray(v) && v.length);
    const reqHint = (hasDeps || hasReqs)
      ? `<span style="font-size:0.7rem;color:var(--text3);margin-left:4px">· has requirements</span>` : '';
    return `
      <div class="cap-card">
        <div style="display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:6px">
          <div>
            <div style="font-weight:600;font-size:0.95rem;margin-bottom:2px">${escHtml(p.display_name || p.name || p.pkg_id)}${source}</div>
            ${version}${reqHint}
          </div>
        </div>
        <div style="font-size:0.8rem;color:var(--text2);margin-bottom:8px;line-height:1.4">${escHtml(p.description || p.objective || '')}</div>
        <div style="margin-bottom:8px">${tags}</div>
        ${installedChips}
        <div style="margin-top:auto;padding-top:10px;display:flex;gap:6px">
          <button class="btn btn-ghost btn-sm" onclick="openGalleryDetails('${escHtml(p.pkg_id)}')">Details</button>
          <button class="btn btn-primary btn-sm" onclick="openGalleryInstall('${escHtml(p.pkg_id)}')">Install</button>
        </div>
      </div>
    `;
  }).join('');
}

function openGalleryImport() {
  document.getElementById('gallery-import-json').value = '';
  document.getElementById('gallery-import-error').style.display = 'none';
  document.getElementById('gallery-import-modal').classList.add('open');
}
function closeGalleryImport() {
  document.getElementById('gallery-import-modal').classList.remove('open');
}
async function submitGalleryImport() {
  const errEl = document.getElementById('gallery-import-error');
  errEl.style.display = 'none';
  let pkg;
  try {
    pkg = JSON.parse(document.getElementById('gallery-import-json').value);
  } catch(e) {
    errEl.textContent = 'Invalid JSON: ' + e.message;
    errEl.style.display = 'block';
    return;
  }
  try {
    const d = await api('POST', '/api/gallery/import', pkg);
    if (d.error) {
      errEl.textContent = d.error;
      errEl.style.display = 'block';
      return;
    }
    closeGalleryImport();
    await loadGallery();
  } catch(e) {
    errEl.textContent = e.message;
    errEl.style.display = 'block';
  }
}

// ── Share modal — Session 4a / v7-arc §9.1 within-pod sharing ────────────────
let _shareSourceBot = null;
let _shareAppId = null;

async function openShareModal(botId, appId, appName) {
  _shareSourceBot = botId;
  _shareAppId = appId;
  document.getElementById('share-modal-title').textContent =
    `Share "${appName || appId}" from ${botId}`;
  document.getElementById('share-modal-error').style.display = 'none';
  document.getElementById('share-modal-success').style.display = 'none';
  const submitBtn = document.getElementById('share-modal-submit');
  submitBtn.disabled = false;
  submitBtn.textContent = 'Share';
  submitBtn.onclick = submitShare;  // reset in case a prior session overrode it

  // Populate target-bot picker from network.json (exclude the source bot).
  const targetEl = document.getElementById('share-target-bots');
  targetEl.innerHTML = '<div style="font-size:0.78rem;color:var(--text3)">Loading bots…</div>';
  try {
    const net = await api('GET', '/api/network');
    const bots = Object.keys((net && net.bots) || {}).filter(b => b !== botId);
    if (bots.length === 0) {
      targetEl.innerHTML = '<div style="font-size:0.78rem;color:var(--text3)">No other bots on this pod.</div>';
    } else {
      targetEl.innerHTML = bots.map(b =>
        `<label style="display:flex;align-items:center;gap:8px;cursor:pointer">
           <input type="radio" name="share-target" value="${escHtml(b)}">
           <span>${escHtml(b)}</span>
         </label>`
      ).join('') +
      `<label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin-top:6px">
         <input type="radio" name="share-target" value="" checked>
         <span style="color:var(--text3)">none — just publish to gallery</span>
       </label>`;
    }
  } catch(e) {
    targetEl.innerHTML = `<div style="color:var(--red);font-size:0.78rem">Couldn't load bot list: ${escHtml(e.message)}</div>`;
  }

  document.getElementById('share-modal').classList.add('open');
}

function closeShareModal() {
  document.getElementById('share-modal').classList.remove('open');
  _shareSourceBot = null;
  _shareAppId = null;
}

async function submitShare() {
  if (!_shareSourceBot || !_shareAppId) return;
  const errEl = document.getElementById('share-modal-error');
  const okEl  = document.getElementById('share-modal-success');
  const submitBtn = document.getElementById('share-modal-submit');
  errEl.style.display = 'none';
  okEl.style.display = 'none';
  submitBtn.disabled = true;
  submitBtn.textContent = 'Sharing…';

  const sel = document.querySelector('input[name="share-target"]:checked');
  const target = sel ? sel.value : '';
  const body = target ? { target_bot_id: target, install: true } : {};

  try {
    const d = await api('POST',
      `/api/applications/${encodeURIComponent(_shareSourceBot)}/${encodeURIComponent(_shareAppId)}/share`,
      body);
    if (d.error) {
      errEl.textContent = d.error;
      errEl.style.display = 'block';
      submitBtn.disabled = false;
      submitBtn.textContent = 'Share';
      return;
    }
    let msg = `Published Spec ${d.spec_id} @ ${d.spec_version}.`;
    if (d.install_job && d.install_job.job_id) {
      msg += ` Install job ${d.install_job.job_id} queued on ${d.install_job.bot_id}.`;
    } else if (d.install_warning) {
      msg += ` (${d.install_warning})`;
    }
    okEl.textContent = msg;
    okEl.style.display = 'block';
    submitBtn.disabled = false;
    submitBtn.textContent = 'Close';
    submitBtn.onclick = () => { closeShareModal(); submitBtn.onclick = submitShare; };
    // Refresh gallery + capabilities so the new shared Spec / install job is visible.
    if (typeof loadGallery === 'function') loadGallery();
    if (typeof loadCapabilities === 'function') loadCapabilities();
  } catch(e) {
    errEl.textContent = e.message;
    errEl.style.display = 'block';
    submitBtn.disabled = false;
    submitBtn.textContent = 'Share';
  }
}

// ── Promote-to-gallery (F-P.7.b) ─────────────────────────────────────────────
// Snapshots an installed app into a candidate gallery files-pack via the
// F-P.7.a endpoint and renders the engine's per-file findings + the
// personalization-scrub totals for operator review. The daemon writes the
// snapshot to /tmp/snapshot-<pkg_id>-…; the operator scp's it back to their
// dev clone and commits via the normal git workflow (matches the F-P.6
// runbook flow — see internal/runbook-snapshot-install-to-gallery-2026-06-03.md).

let _promoteBotId = null, _promotePkgId = null, _promoteName = null;

function openPromoteModal(botId, pkgId, name) {
  _promoteBotId = botId;
  _promotePkgId = pkgId;
  _promoteName = name;
  // "Export to Gallery" — renamed from "Promote" so the word "Promote" means
  // the Defined/Discovered definition-promote everywhere (spec §9.6 bite 4 #4).
  document.getElementById('promote-modal-title').textContent =
    `Export “${name || pkgId}” to the gallery`;
  const statusEl = document.getElementById('promote-modal-status');
  statusEl.innerHTML = `<div>Source bot: <b>${escHtml(botId)}</b></div>
    <div>Package: <code style="font-size:0.78rem;background:var(--bg3);padding:1px 6px;border-radius:4px">${escHtml(pkgId)}</code></div>
    <div style="margin-top:6px;color:var(--text3)">Click <b>Run snapshot</b> to stage a candidate files-pack for review.</div>`;
  document.getElementById('promote-modal-result').style.display = 'none';
  document.getElementById('promote-modal-result').innerHTML = '';
  document.getElementById('promote-modal-error').style.display = 'none';
  const btn = document.getElementById('promote-modal-run');
  btn.disabled = false;
  btn.textContent = 'Run snapshot';
  btn.onclick = runPromoteSnapshot;
  document.getElementById('promote-modal').classList.add('open');
}

function closePromoteModal() {
  document.getElementById('promote-modal').classList.remove('open');
  _promoteBotId = null;
  _promotePkgId = null;
  _promoteName = null;
}

async function runPromoteSnapshot() {
  if (!_promoteBotId || !_promotePkgId) return;
  const btn = document.getElementById('promote-modal-run');
  const errEl = document.getElementById('promote-modal-error');
  const resEl = document.getElementById('promote-modal-result');
  const statusEl = document.getElementById('promote-modal-status');
  errEl.style.display = 'none';
  resEl.style.display = 'none';
  btn.disabled = true;
  btn.textContent = 'Snapshotting…';
  statusEl.innerHTML = `<div class="spinner" style="display:inline-block;width:12px;height:12px;border-width:1.5px;vertical-align:middle"></div>
    <span style="margin-left:6px">Snapshotting ${escHtml(_promoteBotId)}/${escHtml(_promotePkgId)}…</span>`;

  try {
    const d = await api('POST', '/api/gallery/promote/snapshot', {
      bot_id: _promoteBotId,
      pkg_id: _promotePkgId,
      auto_detect: true,
      scrub_personalization: true,
    });
    if (!d.ok) {
      errEl.textContent = 'Snapshot failed: ' + (d.error || 'unknown error');
      errEl.style.display = 'block';
      statusEl.innerHTML = '<div style="color:var(--red)">✗ Snapshot did not run</div>';
      btn.disabled = false;
      btn.textContent = 'Try again';
      return;
    }
    _renderPromoteResult(d);
    statusEl.innerHTML = `<div style="color:var(--green)">✓ Snapshot complete — ${d.files_count} file(s)</div>`;
    btn.textContent = 'Close';
    btn.onclick = closePromoteModal;
  } catch (e) {
    errEl.textContent = 'Request failed: ' + (e && e.message ? e.message : String(e));
    errEl.style.display = 'block';
    statusEl.innerHTML = '<div style="color:var(--red)">✗ Request did not complete</div>';
    btn.disabled = false;
    btn.textContent = 'Try again';
  }
}

// F-P.7.b.x — track per-file provenance toggles for the open promote
// modal. Defaults every snapshotted file to "bundled" (matches today's
// snapshot semantics); operator clicks the badge to flip to "forge".
// The state drives the "Suggested promote-app command" footer.
let _promoteProvenance = {};  // { path: "bundled" | "forge" }

function _renderPromoteResult(d) {
  const resEl = document.getElementById('promote-modal-result');
  const esc = escHtml;
  const out = [];

  // Reset provenance state for this promotion. Default everything bundled.
  _promoteProvenance = {};
  for (const f of (d.per_file || [])) {
    if (f.path) _promoteProvenance[f.path] = 'bundled';
  }

  // Header — staging path + top-level SHA + snapshot pkg_version source.
  out.push(`<div style="font-weight:600;margin-bottom:6px">Staging directory</div>`);
  out.push(`<div style="font-family:monospace;font-size:0.78rem;background:var(--bg3);padding:6px 10px;border-radius:4px;margin-bottom:10px;word-break:break-all">${esc(d.out_dir || '')}</div>`);
  out.push(`<div style="font-size:0.74rem;color:var(--text3);margin-bottom:14px">scp this dir back to your dev clone → place into <code>gallery/&lt;slug&gt;/files/</code> → stamp <code>files_pack</code> on the package manifest → commit. See runbook for the full flow.</div>`);

  // Personalization totals (F-P.8). Pod-wide aggregate above the per-file
  // section so operators can spot false positives at a glance.
  const totals = d.personalization_totals || {};
  const totalKeys = Object.keys(totals);
  if (totalKeys.length > 0) {
    out.push(`<div style="font-weight:600;margin-bottom:6px;margin-top:8px">Personalization scrub (verify before commit)</div>`);
    out.push('<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px">');
    for (const k of totalKeys.sort()) {
      out.push(`<span class="badge" style="background:var(--bg3);color:var(--text2);font-family:monospace">{${esc(k)}}: ${totals[k]}</span>`);
    }
    out.push('</div>');
  }

  // Per-file findings + provenance toggles.
  const files = d.per_file || [];
  if (files.length > 0) {
    out.push(`<div style="font-weight:600;margin-bottom:6px;display:flex;align-items:center;gap:8px">Files (${files.length}) <span style="font-size:0.7rem;color:var(--text3);font-weight:normal">click <span class="badge" style="background:var(--green);color:var(--bg1);font-family:monospace;font-size:0.65rem">bundled</span> ↔ <span class="badge" style="background:var(--yellow);color:var(--bg1);font-family:monospace;font-size:0.65rem">forge</span> to flip provenance</span></div>`);
    out.push('<div style="display:flex;flex-direction:column;gap:6px">');
    for (const f of files) {
      const placeholders = (f.placeholders || []).join(', ');
      const bareNote = f.bare_token_count > 0
        ? ` <span style="color:var(--text3)">(${f.bare_token_count} bare bot_id substitution${f.bare_token_count !== 1 ? 's' : ''})</span>`
        : '';
      const personal = f.personalization || {};
      const personalKeys = Object.keys(personal);
      const personalNote = personalKeys.length > 0
        ? ` <span style="color:var(--text3)">· ${personalKeys.map(k => '{' + k + '}: ' + personal[k]).join(', ')}</span>`
        : '';
      const phLine = placeholders
        ? `<div style="font-size:0.74rem;color:var(--text2);margin-top:2px">↳ ${esc(placeholders)}${bareNote}${personalNote}</div>`
        : `<div style="font-size:0.74rem;color:var(--text3);margin-top:2px">↳ no substitutions</div>`;
      const provId = 'prov-' + esc(f.path).replace(/[^a-zA-Z0-9]/g, '_');
      out.push(`<div style="border:1px solid var(--border);border-radius:6px;padding:8px 12px;background:var(--bg1);display:flex;align-items:center;gap:10px">
        <div style="flex:1;min-width:0">
          <div style="font-family:monospace;font-size:0.78rem;word-break:break-all">${esc(f.path)}</div>
          ${phLine}
        </div>
        <button id="${provId}" class="badge" data-path="${esc(f.path)}" onclick="_togglePromoteProvenance('${esc(f.path).replace(/'/g, "\\'")}', '${provId}')" style="background:var(--green);color:var(--bg1);font-family:monospace;font-size:0.7rem;border:none;cursor:pointer;padding:3px 9px;white-space:nowrap">bundled</button>
      </div>`);
    }
    out.push('</div>');
  }

  // Skipped files (couldn't be read).
  const skipped = d.skipped || [];
  if (skipped.length > 0) {
    out.push(`<div style="font-weight:600;margin-top:14px;margin-bottom:6px;color:var(--yellow)">Skipped (${skipped.length})</div>`);
    out.push('<ul style="margin:0 0 0 18px;padding:0;font-size:0.78rem">');
    for (const s of skipped) {
      out.push(`<li><code>${esc(s.path)}</code> — ${esc(s.reason || 'unknown')}</li>`);
    }
    out.push('</ul>');
  }

  // Suggested promote-app command — built from the current provenance
  // state. Operator clicks Copy + pastes into the daemon ssh session.
  out.push(`<div id="promote-suggested-command-wrap" style="margin-top:16px"></div>`);

  resEl.innerHTML = out.join('');
  resEl.style.display = 'block';
  _renderPromoteSuggestedCommand();
}

function _togglePromoteProvenance(path, btnId) {
  const cur = _promoteProvenance[path] || 'bundled';
  const next = cur === 'bundled' ? 'forge' : 'bundled';
  _promoteProvenance[path] = next;
  const btn = document.getElementById(btnId);
  if (btn) {
    btn.textContent = next;
    btn.style.background = (next === 'bundled') ? 'var(--green)' : 'var(--yellow)';
  }
  _renderPromoteSuggestedCommand();
}

function _renderPromoteSuggestedCommand() {
  const wrap = document.getElementById('promote-suggested-command-wrap');
  if (!wrap) return;
  const bundledPaths = Object.keys(_promoteProvenance)
    .filter(p => _promoteProvenance[p] === 'bundled')
    .sort();
  const allBundled = bundledPaths.length === Object.keys(_promoteProvenance).length;
  const esc = escHtml;
  // Header.
  let html = `<div style="font-weight:600;margin-bottom:6px">Suggested promote-app command</div>`;
  if (Object.keys(_promoteProvenance).length === 0) {
    wrap.innerHTML = '';
    return;
  }
  // Build the command.
  let cmd = `sudo evolve-admin promote-app --bot ${_promoteBotId} --pkg ${_promotePkgId}`;
  if (!allBundled) {
    for (const p of bundledPaths) {
      cmd += ` \\\n  --bundle-only '${p.replace(/'/g, "'\\''")}'`;
    }
  }
  // Render as monospace block with a Copy button.
  html += `<div style="font-family:monospace;font-size:0.74rem;background:var(--bg3);padding:8px 12px;border-radius:4px;white-space:pre-wrap;word-break:break-all;line-height:1.5">${esc(cmd)}</div>`;
  if (allBundled) {
    html += `<div style="font-size:0.72rem;color:var(--text3);margin-top:4px">All files marked bundled — no --bundle-only filters needed.</div>`;
  } else {
    const forgeCount = Object.values(_promoteProvenance).filter(v => v === 'forge').length;
    html += `<div style="font-size:0.72rem;color:var(--text3);margin-top:4px">${forgeCount} file${forgeCount !== 1 ? 's' : ''} marked forge — LLM-generated at install time. Files-pack will be stamped <code>partial: true</code>.</div>`;
  }
  html += `<button class="btn btn-ghost btn-sm" style="margin-top:8px" onclick="_copyPromoteCommand()">📋 Copy command</button>`;
  wrap.innerHTML = html;
}

function _copyPromoteCommand() {
  const bundledPaths = Object.keys(_promoteProvenance)
    .filter(p => _promoteProvenance[p] === 'bundled')
    .sort();
  const allBundled = bundledPaths.length === Object.keys(_promoteProvenance).length;
  let cmd = `sudo evolve-admin promote-app --bot ${_promoteBotId} --pkg ${_promotePkgId}`;
  if (!allBundled) {
    for (const p of bundledPaths) {
      cmd += ` \\\n  --bundle-only '${p.replace(/'/g, "'\\''")}'`;
    }
  }
  navigator.clipboard.writeText(cmd).then(() => {
    if (typeof toast === 'function') toast('✓ Copied', 'ok');
  }).catch(() => {
    if (typeof toast === 'function') toast('✗ Copy failed — select + Cmd-C', 'err');
  });
}

function openGalleryInstall(pkgId) {
  _galleryInstallPkgId = pkgId;
  const pkg = _galleryData.find(p => p.pkg_id === pkgId);
  if (!pkg) return;

  document.getElementById('gallery-install-title').textContent = 'Install: ' + (pkg.display_name || pkg.name || pkgId);
  document.getElementById('gallery-install-objective').textContent = pkg.objective || '';
  document.getElementById('gallery-install-error').style.display = 'none';
  document.getElementById('gallery-install-success').style.display = 'none';

  // F-P.12.c — community-source confirmation gate. When the package
  // came from an external operator (provenance="community"), block the
  // Install button until the operator acknowledges the trust risk.
  // evolve / operator-local skip the gate entirely.
  _renderCommunityTrustGate(pkg);

  // Populate bot checkboxes
  const botsEl = document.getElementById('gallery-install-bots');
  const installedOn = new Set(pkg.installed_on || []);
  // Collect bot ids from the cached status data (members only, not admin bot)
  const statusBots = Object.entries(_statusData?.bots || {})
    .filter(([, v]) => v.role !== 'primary')
    .map(([k]) => k);
  const allBots = Array.from(new Set([
    ...statusBots,
    ...Array.from(installedOn),
  ]));
  if (!allBots.length) {
    botsEl.innerHTML = '<span style="color:var(--text3);font-size:0.8rem">No bots configured.</span>';
  } else {
    botsEl.innerHTML = allBots.map(b => {
      const checked = installedOn.has(b) ? '' : 'checked';
      const label = installedOn.has(b) ? b + ' <span class="badge badge-ok">installed</span>' : b;
      return `<label style="display:flex;align-items:center;gap:8px;cursor:pointer">
        <input type="checkbox" name="install-bot" value="${escHtml(b)}" ${checked} style="width:auto">
        <span style="font-size:0.82rem">${label}</span>
      </label>`;
    }).join('');
  }

  document.getElementById('gallery-install-modal').classList.add('open');
}
function closeGalleryInstall() {
  document.getElementById('gallery-install-modal').classList.remove('open');
  document.getElementById('gallery-install-preflight').style.display = 'none';
  const fw = document.getElementById('gallery-install-force-wrap');
  if (fw) fw.style.display = 'none';
  const fc = document.getElementById('gallery-install-force');
  if (fc) fc.checked = false;
  // F-P.12.c — clear the trust gate slot + reset acknowledgement so a
  // re-open starts fresh.
  const slot = document.getElementById('gallery-install-trust-gate');
  if (slot) slot.innerHTML = '';
  _communityTrustAcknowledged = false;
  const btn = document.getElementById('gallery-install-btn');
  if (btn) btn.disabled = false;
  _galleryInstallPkgId = null;
}

// F-P.12.c — community-install trust gate.
//
// When the package's provenance is "community", render a yellow warning
// panel above the bot picker. The Install button is disabled until the
// operator checks the "I understand" box. evolve / operator-local
// packages skip the gate (full trust by default).
//
// The gate is informational + blocking, not destructive: closing the
// modal cancels the install. Each install gets a fresh acknowledgement
// (no persistent "trust this contributor" — that's F-P.12.d territory).

let _communityTrustAcknowledged = false;

function _renderCommunityTrustGate(pkg) {
  const slot = document.getElementById('gallery-install-trust-gate');
  const btn = document.getElementById('gallery-install-btn');
  // Recover the provenance — defensive derivation matches F-P.12.b.
  let provenance = pkg.provenance || '';
  if (!provenance) {
    provenance = (pkg.source === 'imported') ? 'community' : 'evolve';
  }

  if (provenance !== 'community') {
    // evolve / operator-local — no gate. Make sure the slot is hidden +
    // the install button is in its normal enabled state.
    if (slot) slot.innerHTML = '';
    if (btn) btn.disabled = false;
    _communityTrustAcknowledged = true;  // gate doesn't apply
    return;
  }

  // Community — render the gate, disable install until acknowledged.
  _communityTrustAcknowledged = false;
  if (btn) btn.disabled = true;
  if (!slot) return;

  // F-P.12.d — contributor identity. When a community package declares
  // a contributor block, surface the attribution so the operator can
  // decide based on the source, not just the trust tier.
  const c = pkg.contributor || {};
  let contributorLine = '';
  if (c.name) {
    const nameDisplay = c.url
      ? `<a href="${escHtml(c.url)}" target="_blank" rel="noopener noreferrer" style="color:var(--accent);text-decoration:none;border-bottom:1px dotted var(--accent)">${escHtml(c.name)}</a>`
      : escHtml(c.name);
    const handlePart = c.handle ? ` <span style="color:var(--text3);font-size:0.72rem">(@${escHtml(c.handle)})</span>` : '';
    contributorLine = `<div style="font-size:0.78rem;color:var(--text2);margin-bottom:8px"><span style="color:var(--text3)">Contributed by</span> <span style="font-weight:600;color:var(--text)">${nameDisplay}</span>${handlePart}</div>`;
  }

  slot.innerHTML = `
    <div style="padding:10px 14px;background:var(--bg3);border:1px solid var(--yellow);border-radius:6px;margin-bottom:12px">
      <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
        <span style="font-size:1.1rem">⚠️</span>
        <div style="font-weight:700;font-size:0.85rem;color:var(--text)">Community-contributed app</div>
      </div>
      ${contributorLine}
      <div style="font-size:0.78rem;color:var(--text2);line-height:1.5;margin-bottom:10px">
        This app was contributed by an external operator. It runs on your bot
        with the same privileges as any Evolve-shipped app, including access to
        your bot's LLM credentials and workspace. Review the manifest before
        installing.
      </div>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:0.78rem;color:var(--text)">
          <input type="checkbox" id="community-trust-ack" onchange="_onCommunityTrustToggle()" style="width:auto">
          <span>I understand this app came from outside Evolve.</span>
        </label>
        <button class="btn btn-ghost btn-sm" onclick="openGalleryDetails('${escHtml(pkg.pkg_id)}')">📋 Review manifest</button>
      </div>
    </div>`;
}

function _onCommunityTrustToggle() {
  const cb = document.getElementById('community-trust-ack');
  const btn = document.getElementById('gallery-install-btn');
  _communityTrustAcknowledged = !!(cb && cb.checked);
  if (btn) btn.disabled = !_communityTrustAcknowledged;
}

// Renders the cost-confirmation panel in the install modal's success area
// when /api/gallery/<pkg>/install returns 412 with requires_confirmation.
// Reuses the success area's display block to keep the modal's height
// consistent.
function _renderGalleryCostConfirm(projections) {
  const okEl = document.getElementById('gallery-install-success');
  if (!okEl) return;
  const fmt = (n) => `$${(Number(n) || 0).toFixed(2)}`;
  const rows = projections.map(p => {
    const flag = p.exceeds_threshold
      ? ` <span style="color:var(--red);font-weight:700">⚠ over $${(Number(p.threshold_usd)||0).toFixed(0)} threshold</span>`
      : '';
    return `<tr>
      <td style="padding:3px 6px;font-weight:600">${escHtml(botLabel(p.bot_id))}</td>
      <td style="padding:3px 6px;text-align:right">${fmt(p.mid_usd)}</td>
      <td style="padding:3px 6px;text-align:right;color:var(--text3);font-size:0.75rem">${fmt(p.low_usd)} – ${fmt(p.high_usd)}${flag}</td>
    </tr>`;
  }).join('');
  okEl.style.display = 'block';
  okEl.style.color = 'var(--text)';  // reset green styling for confirmation use
  okEl.innerHTML = `
    <div style="padding:10px 12px;background:var(--bg3);border:1px solid var(--border);border-radius:6px">
      <div style="font-size:0.82rem;font-weight:700;color:var(--text);margin-bottom:6px">Confirm install cost</div>
      <table style="width:100%;font-size:0.8rem;border-collapse:collapse"><tbody>${rows}</tbody></table>
      <div style="margin-top:6px;font-size:0.7rem;color:var(--text3)">Estimate — actual cost varies. Operator-confirmed installs are exempt from <code>daily_cap_usd</code> by default.</div>
      <div style="margin-top:10px;display:flex;gap:8px;justify-content:flex-end">
        <button class="btn btn-primary btn-sm" onclick="submitGalleryInstall({confirmed:true})">Confirm install</button>
      </div>
    </div>`;
  // Re-enable the original Install button so the user can also bail by
  // pressing Cancel.
  const btn = document.getElementById('gallery-install-btn');
  if (btn) btn.disabled = false;
}

async function submitGalleryInstall(opts) {
  if (!_galleryInstallPkgId) return;
  const errEl = document.getElementById('gallery-install-error');
  const okEl  = document.getElementById('gallery-install-success');
  errEl.style.display = 'none';
  okEl.style.display  = 'none';

  const checked = Array.from(
    document.querySelectorAll('#gallery-install-bots input[name="install-bot"]:checked')
  ).map(el => el.value);

  if (!checked.length) {
    errEl.textContent = 'Select at least one bot.';
    errEl.style.display = 'block';
    return;
  }

  const force = document.getElementById('gallery-install-force')?.checked || false;
  const confirmed = !!(opts && opts.confirmed);
  const btn = document.getElementById('gallery-install-btn');
  btn.disabled = true;
  try {
    const d = await api('POST', `/api/gallery/${_galleryInstallPkgId}/install`, {
      bot_ids: checked,
      force,
      confirmed,
    });

    // 412 path: the projected cost exceeded one of the bots'
    // auto-approve thresholds. Render the projection in the success area
    // (used here as a confirmation panel) with a Confirm install button
    // that re-submits with confirmed=true.
    if (d && d.requires_confirmation) {
      _renderGalleryCostConfirm(d.projections || []);
      return;
    }

    if (d.error) {
      errEl.textContent = d.error;
      errEl.style.display = 'block';
      return;
    }
    // jobs array contains successful jobs, awaiting_oauth pauses, and preflight-blocked entries
    const allResults = d.jobs || [];
    const awaitingOauth = allResults.filter(j => j.status === 'awaiting_oauth');
    const blocked = allResults.filter(j => j.blocked);
    const queued  = allResults.filter(j => !j.blocked && j.status !== 'awaiting_oauth');

    if (blocked.length) {
      const msgs = blocked.map(b => {
        const pf = b.preflight || {};
        const blockerItems = [
          ...(pf.app_dependencies || []),
          ...(pf.requirements || []),
        ].filter(x => x.severity === 'build_blocker' && x.state !== 'satisfied');
        const detail = blockerItems.map(x => (x.display_name || x.id || '')).filter(Boolean).join(', ');
        return `${b.bot_id}: missing ${detail || 'requirements'}`;
      }).join('\n');
      errEl.textContent = `Blocked for some bots (check requirements):\n${msgs}`;
      errEl.style.display = 'block';
    }

    // V2.1-4: awaiting_oauth results — render in-line OAuth action panel using
    // the shared helper.  The forge job was created in awaiting_oauth state;
    // the sweeper will resume it once the operator completes OAuth.
    if (awaitingOauth.length) {
      // Show the awaiting_oauth panel inside the install modal's success area
      okEl.innerHTML = '';
      okEl.style.display = 'block';
      awaitingOauth.forEach(res => {
        // Renamed from "botLabel" to avoid shadowing the global helper
        // (which we use immediately below).
        const botHeader = document.createElement('div');
        botHeader.style.cssText = 'font-weight:600;font-size:0.85rem;margin-bottom:6px';
        botHeader.textContent = `${botLabel(res.bot_id)}: OAuth setup needed`;
        okEl.appendChild(botHeader);

        const planDiv = document.createElement('div');
        okEl.appendChild(planDiv);

        const jobId = res.job_id;
        _renderAwaitingOauthInstallPlan(
          res.missing || [],
          /* statusEndpoint */ `/api/forge/jobs/${jobId}`,
          /* onResume */ () => {
            botLabel.textContent = `${res.bot_id}: OAuth complete — install will resume shortly`;
            botLabel.style.color = 'var(--green)';
            loadGallery();
          },
          /* onTimeout */ () => {
            botLabel.textContent = `${res.bot_id}: check Forge Jobs for progress`;
          },
          /* renderTarget */ planDiv,
          /* botId       */ res.bot_id,
        );
      });
    }

    if (queued.length) {
      const queuedMsg = document.createElement('div');
      queuedMsg.textContent = `Created ${queued.length} forge job(s). Check Forge Jobs for progress.`;
      if (awaitingOauth.length) {
        // Append below the oauth panel
        okEl.appendChild(queuedMsg);
      } else {
        okEl.textContent = queuedMsg.textContent;
        okEl.style.display = 'block';
        setTimeout(() => { loadGallery(); closeGalleryInstall(); }, 1800);
      }
    }
  } catch(e) {
    errEl.textContent = e.message;
    errEl.style.display = 'block';
  } finally {
    btn.disabled = false;
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// GALLERY — DETAILS MODAL + PREFLIGHT
// ═══════════════════════════════════════════════════════════════════════════

let _galleryDetailsPkgId = null;

// Inline setup guidance for common integrations and tools
const _SETUP_GUIDANCE = {
  google_calendar: `Add to openclaw.json → integrations:\n  "google_calendar": { "token": "...", "refresh_token": "...", "client_id": "...", "client_secret": "..." }`,
  gmail:           `Add to openclaw.json → integrations:\n  "gmail": { "token": "...", "refresh_token": "...", "client_id": "...", "client_secret": "..." }`,
  github:          `Add to openclaw.json → integrations:\n  "github": { "token": "ghp_...", "repos": ["owner/repo"] }`,
  git:             `Install git: brew install git`,
};

// _galleryFullManifests — cache of full-manifest fetches by pkg_id, so re-
// opening the same Details modal doesn't re-hit /api/gallery/<id>. Cleared
// when the gallery list reloads (renderGallery treats this cache as ground
// truth for the modal session, not as durable state).
let _galleryFullManifests = {};

function openGalleryDetails(pkgId) {
  _galleryDetailsPkgId = pkgId;
  const pkg = _galleryData.find(p => p.pkg_id === pkgId);
  if (!pkg) return;

  // Header — populated immediately from the thin index record so the modal
  // pops fast. The rich body is async-fetched below.
  document.getElementById('gd-title').textContent = pkg.display_name || pkg.name || pkgId;
  const metaParts = [];
  if (pkg.pkg_version) metaParts.push(`<span style="color:var(--text3)">v${escHtml(pkg.pkg_version)}</span>`);
  if (pkg.source === 'imported') metaParts.push(`<span class="badge badge-inline">imported</span>`);
  (pkg.tags || []).forEach(t => {
    const kind = _galleryTagKinds[t] || 'freeform';
    const active = _galleryTagFilter.has(t) ? ' gtag-chip-active' : '';
    metaParts.push(
      `<span class="gtag-chip gtag-chip-${kind} gtag-chip-btn${active}"
         onclick="closeGalleryDetails();toggleGalleryTagFilter('${escHtml(t)}')"
         title="${escHtml(kind)} tag — click to filter">${escHtml(t)}</span>`
    );
  });
  document.getElementById('gd-meta').innerHTML = metaParts.join('');

  // Body — render with the thin record first so the operator sees something
  // immediately, then re-render with the full manifest as soon as it arrives.
  // The thin record carries description + tags + installed_on; everything
  // richer (identity, scope_includes, scheduled_actions, blueprint.files,
  // success_criteria, constraints, interface_contract, example_triggers,
  // test_cases) only exists in the full manifest at /api/gallery/<pkg_id>.
  document.getElementById('gd-body').innerHTML = _renderGalleryDetailsBody(pkg, null);
  document.getElementById('gallery-details-modal').classList.add('open');

  const cached = _galleryFullManifests[pkgId];
  if (cached) {
    document.getElementById('gd-body').innerHTML = _renderGalleryDetailsBody(pkg, cached);
    return;
  }
  // Fire the fetch; ignore if the operator closed the modal before it lands.
  api('GET', `/api/gallery/${encodeURIComponent(pkgId)}`).then(full => {
    if (!full || full.error) return;
    _galleryFullManifests[pkgId] = full;
    // Only re-render if THIS modal is still the active one — a fast operator
    // who closed + reopened a different pkg shouldn't see this paint.
    if (_galleryDetailsPkgId === pkgId) {
      document.getElementById('gd-body').innerHTML = _renderGalleryDetailsBody(pkg, full);
    }
  }).catch(() => { /* network error — leave the thin render in place */ });
}

// Render the body of the Gallery Details modal.
//
// Two paint stages:
//   1. With full=null — the thin index record only. Description, tags,
//      installed-on chips, app deps + requirements (these are in the thin
//      record because they drive install affordances), and a "loading
//      details…" placeholder where the richer sections will land.
//   2. With full populated — adds identity, scope, schedule, files,
//      success criteria, constraints, example triggers, test cases.
//
// Sections are ordered by what an operator deciding "should I install this"
// needs to see first: what it is → what it does → when it runs → what it
// leaves on disk → what's required → quality bar → safety → tests.
function _renderGalleryDetailsBody(pkg, full) {
  const installed = pkg.installed_on || [];
  const appDeps = pkg.app_dependencies || [];
  const reqs = pkg.requirements || {};
  let html = '';

  // Description — the short pitch from the index record. Same field as the
  // tile so this stays one paragraph; identity.purpose below adds depth.
  const desc = pkg.description || pkg.objective || '';
  if (desc) {
    html += `<div style="font-size:0.85rem;color:var(--text2);margin-bottom:14px;line-height:1.55">${escHtml(desc)}</div>`;
  }

  // ── Sections from the FULL manifest ──────────────────────────────────────
  if (full) {
    const identity = full.identity || {};

    // About — purpose, who it's for, how the bot interacts with it. Each is
    // a short prose blob; we show the ones the manifest populates.
    const aboutBits = [];
    if (identity.purpose) aboutBits.push(['Purpose', identity.purpose]);
    if (identity.user) aboutBits.push(['Who it\'s for', identity.user]);
    if (identity.bot_interaction_pattern) aboutBits.push(['How the bot uses it', identity.bot_interaction_pattern]);
    if (aboutBits.length) {
      const rows = aboutBits.map(([label, val]) =>
        `<div style="margin-bottom:8px"><div style="font-size:0.72rem;color:var(--text3);margin-bottom:2px">${escHtml(label)}</div>
         <div style="font-size:0.82rem;color:var(--text2);line-height:1.5">${escHtml(val)}</div></div>`
      ).join('');
      html += _gdSection('About', rows);
    }

    // What it does — scope_includes is the operator's checklist of "this app
    // promises to do these things." Rendered as bullets, no truncation.
    if (Array.isArray(identity.scope_includes) && identity.scope_includes.length) {
      const items = identity.scope_includes.map(s => `<li style="margin-bottom:3px">${escHtml(s)}</li>`).join('');
      html += _gdSection('What it does',
        `<ul style="margin:0;padding-left:18px;font-size:0.82rem;color:var(--text2);line-height:1.5">${items}</ul>`
      );
    }
    // What it doesn't do — collapsed by default; the negatives are useful
    // for confidence but rarely the first thing an operator wants.
    if (Array.isArray(identity.scope_excludes) && identity.scope_excludes.length) {
      const items = identity.scope_excludes.map(s => `<li style="margin-bottom:3px">${escHtml(s)}</li>`).join('');
      html += _gdCollapsible(`What it doesn't do (${identity.scope_excludes.length})`,
        `<ul style="margin:6px 0 0;padding-left:18px;font-size:0.82rem;color:var(--text2);line-height:1.5">${items}</ul>`
      );
    }

    // When it runs — summarize scheduled_actions[] one line per entry. We
    // pull mechanism + a human description from install.schedule.* where
    // present, falling back to summary text.
    const sched = full.scheduled_actions || [];
    if (sched.length) {
      const rows = sched.map(sa => {
        const mech = sa.mechanism || (sa.trigger && sa.trigger.kind) || 'scheduled';
        const when = _gdScheduleHuman(sa);
        const summary = sa.summary || '';
        return `<div style="padding:6px 0;border-bottom:1px solid var(--border)">
          <div style="font-size:0.82rem;color:var(--text2)"><code style="background:var(--bg3);padding:1px 6px;border-radius:3px;font-size:0.74rem">${escHtml(mech)}</code> ${escHtml(when)}</div>
          ${summary ? `<div style="font-size:0.75rem;color:var(--text3);margin-top:2px">${escHtml(summary)}</div>` : ''}
        </div>`;
      }).join('');
      html += _gdSection('When it runs', rows);
    }

    // Example prompts — only render when present. Useful for interactive apps
    // (e.g. "What unread emails do I have?"); cron-driven apps usually leave
    // this empty.
    const examples = full.example_triggers || [];
    if (examples.length) {
      const items = examples.slice(0, 8).map(e =>
        `<li style="margin-bottom:3px"><code style="font-size:0.78rem;color:var(--text2)">${escHtml(e)}</code></li>`
      ).join('');
      const more = examples.length > 8 ? `<div style="font-size:0.72rem;color:var(--text3);margin-top:4px">+${examples.length - 8} more</div>` : '';
      html += _gdSection('Example prompts',
        `<ul style="margin:0;padding-left:18px;list-style:'›  '">${items}</ul>${more}`
      );
    }

    // Files created — blueprint.files[] (intent + role + expected_location).
    // We intentionally do NOT show code_snippet or any file content; that's
    // forge's job at install time, and the modal would bloat hard.
    const files = (full.blueprint && full.blueprint.files) || [];
    if (files.length) {
      const rows = files.map(f => {
        const roleBadge = _gdRoleBadge(f.role);
        const path = f.expected_location || f.logical_name || '?';
        const intent = f.intent || '';
        return `<div style="padding:6px 0;border-bottom:1px solid var(--border)">
          <div style="display:flex;align-items:center;gap:6px;font-size:0.78rem">
            <code style="background:var(--bg3);padding:2px 6px;border-radius:3px;font-family:monospace">${escHtml(path)}</code>
            ${roleBadge}
          </div>
          ${intent ? `<div style="font-size:0.75rem;color:var(--text3);margin-top:3px">${escHtml(intent)}</div>` : ''}
        </div>`;
      }).join('');
      html += _gdSection(`Files this app creates (${files.length})`, rows);
    }

    // Data files written — interface_contract.data_files. Path + description;
    // field schema goes into a collapsed sub-element so the surface stays
    // calm and the operator can drill in for shape detail.
    const ic = full.interface_contract || {};
    const dataFiles = ic.data_files || [];
    if (dataFiles.length) {
      const rows = dataFiles.map(d => {
        const path = d.path || '?';
        const description = d.description || '';
        const schema = d.schema || {};
        const fields = schema.fields || {};
        const fieldKeys = Object.keys(fields);
        const schemaHtml = fieldKeys.length
          ? _gdCollapsible(`Schema (${fieldKeys.length} field${fieldKeys.length===1?'':'s'})`,
              `<div style="font-size:0.74rem;color:var(--text3);margin-top:4px"><div>${escHtml(schema.storage_format || '')}</div>` +
              fieldKeys.map(k => `<div style="margin-top:3px"><code style="font-family:monospace">${escHtml(k)}</code>: <span>${escHtml(fields[k])}</span></div>`).join('') +
              `</div>`,
              {small: true})
          : '';
        return `<div style="padding:6px 0;border-bottom:1px solid var(--border)">
          <div style="font-size:0.78rem"><code style="background:var(--bg3);padding:2px 6px;border-radius:3px;font-family:monospace">${escHtml(path)}</code></div>
          ${description ? `<div style="font-size:0.75rem;color:var(--text3);margin-top:3px">${escHtml(description)}</div>` : ''}
          ${schemaHtml}
        </div>`;
      }).join('');
      html += _gdSection('Data files this app writes', rows);
    }

    // CLI commands — operator can run these directly to drive the app.
    // Short list, often 2-5 commands.
    const cli = ic.cli || [];
    if (cli.length) {
      const items = cli.map(c => {
        const cmd = c.command || '';
        const flags = (c.key_flags || []).filter(Boolean);
        const flagsLine = flags.length ? `<div style="font-size:0.72rem;color:var(--text3);margin-top:2px;padding-left:14px">${flags.map(f => `<code style="font-family:monospace">${escHtml(f)}</code>`).join(' ')}</div>` : '';
        return `<div style="padding:5px 0;border-bottom:1px solid var(--border)">
          <code style="font-family:monospace;font-size:0.78rem;color:var(--text2)">${escHtml(cmd)}</code>
          ${flagsLine}
        </div>`;
      }).join('');
      html += _gdSection('Commands you can run', items);
    }
  }

  // ── Always rendered (from index record, even before full lands) ─────────

  // Installed on
  if (installed.length) {
    html += `<div style="margin-bottom:14px">
      <div style="font-size:0.72rem;text-transform:uppercase;letter-spacing:0.07em;color:var(--text3);margin-bottom:5px">Installed on</div>
      <div style="display:flex;flex-wrap:wrap;gap:4px">
        ${installed.map(b => `<span class="badge badge-ok">${escHtml(botLabel(b))}</span>`).join('')}
      </div>
    </div>`;
  }

  // App dependencies
  if (appDeps.length) {
    const rows = appDeps.map(d => {
      const badge = d.required
        ? `<span class="badge badge-crit" style="font-size:0.62rem">required</span>`
        : `<span class="badge badge-inline" style="font-size:0.62rem">optional</span>`;
      return `<div style="padding:6px 0;border-bottom:1px solid var(--border)">
        <div style="font-size:0.85rem;font-weight:500;display:flex;align-items:center;gap:6px">${escHtml(d.display_name || d.pkg_id)} ${badge}</div>
        ${d.reason ? `<div style="font-size:0.75rem;color:var(--text3);margin-top:2px">${escHtml(d.reason)}</div>` : ''}
      </div>`;
    }).join('');
    html += _gdSection('App dependencies', rows);
  }

  // Requirements (integrations + secrets + system + python_packages +
  // files + messaging_channel).
  // When the full manifest is loaded, integration entries carry richer
  // fields (check_path / setup_doc / alternatives) than the thin record;
  // surface them so the operator knows where to wire things up.
  const allReqs = [
    ...(reqs.integrations      || []).map(r => ({...r, _type:'integration'})),
    ...(reqs.secrets           || []).map(r => ({...r, _type:'secret'})),
    ...(reqs.system            || []).map(r => ({...r, _type:'system'})),
    ...(reqs.python_packages   || []).map(r => ({...r, _type:'python_package'})),
    ...(reqs.files             || []).map(r => ({...r, _type:'file'})),
    ...(reqs.messaging_channel || []).map(r => ({...r, _type:'messaging_channel'})),
  ];
  if (allReqs.length) {
    const TYPE_LABELS = {integration:'Integration', secret:'Secret', system:'System tool', python_package:'Python package', file:'File', messaging_channel:'Messaging channel'};
    const rows = allReqs.map(r => {
      const label = r.id || r.name || r.key || r.pip_name || r.import || r.path || '';
      const badge = r.required
        ? `<span class="badge badge-crit" style="font-size:0.62rem">required</span>`
        : `<span class="badge badge-inline" style="font-size:0.62rem">optional</span>`;
      const typeLabel = TYPE_LABELS[r._type] || r._type;
      const extra = [];
      if (r.check_path)  extra.push(`<div>Check: <code style="font-family:monospace">${escHtml(r.check_path)}</code></div>`);
      if (r.path)        extra.push(`<div>Path: <code style="font-family:monospace">${escHtml(r.path)}</code>${r.mode ? ` (mode ${escHtml(r.mode)})` : ''}</div>`);
      if (r.setup_doc)   extra.push(`<div>Setup: <code style="font-family:monospace">${escHtml(r.setup_doc)}</code></div>`);
      if (Array.isArray(r.alternatives) && r.alternatives.length) {
        const altIds = r.alternatives.map(a => a.id).filter(Boolean).join(', ');
        if (altIds) extra.push(`<div>Alternatives: ${escHtml(altIds)}</div>`);
      }
      const extraLine = extra.length ? `<div style="font-size:0.72rem;color:var(--text3);margin-top:4px;line-height:1.5">${extra.join('')}</div>` : '';
      return `<div style="padding:6px 0;border-bottom:1px solid var(--border)">
        <div style="font-size:0.85rem;font-weight:500;display:flex;align-items:center;gap:6px">${escHtml(label)} ${badge}</div>
        <div style="font-size:0.75rem;color:var(--text3);margin-top:1px">${escHtml(typeLabel)}${r.reason ? ' — ' + escHtml(r.reason) : ''}</div>
        ${extraLine}
      </div>`;
    }).join('');
    html += _gdSection('Requirements', rows);
  }

  // ── More sections from FULL (quality + safety + tests) ──────────────────
  if (full) {
    const sc = full.success_criteria || {};

    // Quality bar — minimum + excellent. Operator-facing definition of done.
    const qb = sc.quality_bar || {};
    if (qb.minimum || qb.excellent) {
      const parts = [];
      if (qb.minimum)   parts.push(`<div style="margin-bottom:6px"><div style="font-size:0.72rem;color:var(--text3);margin-bottom:2px">Minimum</div><div style="font-size:0.82rem;color:var(--text2);line-height:1.5">${escHtml(qb.minimum)}</div></div>`);
      if (qb.excellent) parts.push(`<div><div style="font-size:0.72rem;color:var(--text3);margin-bottom:2px">Excellent</div><div style="font-size:0.82rem;color:var(--text2);line-height:1.5">${escHtml(qb.excellent)}</div></div>`);
      html += _gdSection('Quality bar', parts.join(''));
    }

    // What success looks like — observable outcomes (these are concrete
    // operator-checkable signals, not vague "works well").
    const outcomes = sc.observable_outcomes || sc.observable || [];
    if (outcomes.length) {
      const items = outcomes.map(o => `<li style="margin-bottom:3px">${escHtml(o)}</li>`).join('');
      html += _gdSection('What success looks like',
        `<ul style="margin:0;padding-left:18px;font-size:0.82rem;color:var(--text2);line-height:1.5">${items}</ul>`
      );
    }
    // What failure looks like — collapsed (negative is useful but secondary).
    const failures = sc.failure_signals || [];
    if (failures.length) {
      const items = failures.map(f => `<li style="margin-bottom:3px">${escHtml(f)}</li>`).join('');
      html += _gdCollapsible(`What failure looks like (${failures.length})`,
        `<ul style="margin:6px 0 0;padding-left:18px;font-size:0.82rem;color:var(--text2);line-height:1.5">${items}</ul>`
      );
    }

    // Safety + boundaries — constraints block (gallery specs split safety
    // from boundaries; we surface both as bullet lists).
    const constraints = full.constraints || {};
    if (Array.isArray(constraints.safety) && constraints.safety.length) {
      const items = constraints.safety.map(s => `<li style="margin-bottom:3px">${escHtml(s)}</li>`).join('');
      html += _gdSection('Safety',
        `<ul style="margin:0;padding-left:18px;font-size:0.82rem;color:var(--text2);line-height:1.5">${items}</ul>`
      );
    }
    if (Array.isArray(constraints.boundaries) && constraints.boundaries.length) {
      const items = constraints.boundaries.map(b => `<li style="margin-bottom:3px">${escHtml(b)}</li>`).join('');
      html += _gdSection('Boundaries',
        `<ul style="margin:0;padding-left:18px;font-size:0.82rem;color:var(--text2);line-height:1.5">${items}</ul>`
      );
    }

    // Test cases — collapsed; trigger + expected per entry. These are
    // operator-authored verification recipes.
    const tcs = full.test_cases || [];
    if (tcs.length) {
      const items = tcs.map(tc => {
        const trig = tc.trigger || tc.id || '';
        const exp = tc.expected || tc.description || '';
        return `<div style="padding:6px 0;border-bottom:1px solid var(--border)">
          ${trig ? `<div style="font-size:0.78rem;color:var(--text2)"><code style="font-family:monospace;font-size:0.76rem">${escHtml(trig)}</code></div>` : ''}
          ${exp  ? `<div style="font-size:0.74rem;color:var(--text3);margin-top:3px">${escHtml(exp)}</div>` : ''}
        </div>`;
      }).join('');
      html += _gdCollapsible(`Test cases (${tcs.length})`, items);
    }
  } else {
    // The full manifest hasn't arrived yet. Show a brief placeholder so the
    // operator knows there's more coming without making it look broken.
    html += `<div style="font-size:0.74rem;color:var(--text3);margin:14px 0 4px;font-style:italic">Loading details…</div>`;
  }

  // Live preflight check — always rendered when there are checkable items.
  const statusBots = Object.entries(_statusData?.bots || {})
    .filter(([, v]) => v.role !== 'primary')
    .map(([k]) => k);
  if (statusBots.length && (appDeps.length || allReqs.length)) {
    const opts = statusBots.map(b => `<option value="${escHtml(b)}">${escHtml(botLabel(b))}</option>`).join('');
    html += `<div style="margin-top:16px;padding:12px;background:var(--bg3);border-radius:8px;border:1px solid var(--border)">
      <div style="font-size:0.72rem;text-transform:uppercase;letter-spacing:0.07em;color:var(--text3);margin-bottom:8px">Live requirements check</div>
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
        <select id="gd-preflight-bot" style="flex:1;min-width:0">
          <option value="">Select a bot…</option>
          ${opts}
        </select>
        <button class="btn btn-ghost btn-sm" onclick="checkGalleryPreflight()">Check</button>
      </div>
      <div id="gd-preflight-results"></div>
    </div>`;
  }

  return html;
}

function _gdSection(title, bodyHtml) {
  return `<div style="margin-bottom:14px">
    <div style="font-size:0.72rem;text-transform:uppercase;letter-spacing:0.07em;color:var(--text3);margin-bottom:5px">${escHtml(title)}</div>
    <div>${bodyHtml}</div>
  </div>`;
}

// Collapsible section — native <details>/<summary>. We pass `small:true` for
// nested ones (per-data-file schemas) so they sit tight inside the parent
// row rather than dominating it.
function _gdCollapsible(title, bodyHtml, opts) {
  const small = opts && opts.small;
  const titleSize  = small ? '0.7rem'  : '0.72rem';
  const margin     = small ? '6px 0 0' : '0 0 14px';
  return `<details style="margin:${margin}">
    <summary style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:${titleSize};text-transform:uppercase;letter-spacing:0.07em;color:var(--text3);user-select:none"><span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>${escHtml(title)}</summary>
    <div style="margin-top:6px">${bodyHtml}</div>
  </details>`;
}

// Map blueprint.files[*].role to a colored chip. Mirrors the schema's enum
// (vital_to_blueprint / instance_specific / reference_only) — collapsing to
// short user-facing labels.
function _gdRoleBadge(role) {
  if (role === 'vital_to_blueprint') {
    return `<span class="badge badge-inline" style="font-size:0.6rem;background:rgba(124,92,255,0.18);color:var(--accent)">code</span>`;
  }
  if (role === 'instance_specific') {
    return `<span class="badge badge-inline" style="font-size:0.6rem;background:rgba(34,197,94,0.15);color:var(--green)">data</span>`;
  }
  if (role === 'reference_only') {
    return `<span class="badge badge-inline" style="font-size:0.6rem;background:var(--bg3);color:var(--text3)">doc</span>`;
  }
  return '';
}

// Render a scheduled_action[*] entry as a one-line human description. We try
// to surface useful structure (every N min, daily at H:M) from the
// install.schedule block; fall back to "scheduled" when the shape is unknown.
function _gdScheduleHuman(sa) {
  const install = sa.install || {};
  const sched = install.schedule || {};
  if (typeof sched.every_minutes === 'number') {
    if (sched.every_minutes % 60 === 0 && sched.every_minutes > 0) {
      const h = sched.every_minutes / 60;
      return `every ${h} hour${h === 1 ? '' : 's'}`;
    }
    return `every ${sched.every_minutes} minute${sched.every_minutes === 1 ? '' : 's'}`;
  }
  const cron = sched.cron || sched.StartCalendarInterval || null;
  if (cron && typeof cron === 'object' && (typeof cron.Hour === 'number' || typeof cron.Minute === 'number')) {
    const h = (typeof cron.Hour === 'number') ? cron.Hour : 0;
    const m = (typeof cron.Minute === 'number') ? cron.Minute : 0;
    const hh = String(h).padStart(2, '0');
    const mm = String(m).padStart(2, '0');
    return `daily at ${hh}:${mm}`;
  }
  if (typeof sched.cron === 'string' && sched.cron.trim()) {
    return `cron: ${sched.cron.trim()}`;
  }
  // Heartbeat schedule lives elsewhere — surface as "every heartbeat" with
  // a hint about cadence if the spec ships one.
  if ((sa.trigger || {}).kind === 'heartbeat') {
    return 'every heartbeat tick';
  }
  return 'on schedule';
}

function closeGalleryDetails() {
  document.getElementById('gallery-details-modal').classList.remove('open');
  _galleryDetailsPkgId = null;
}

function openGalleryInstallFromDetails() {
  const pkgId = _galleryDetailsPkgId;
  closeGalleryDetails();
  if (pkgId) openGalleryInstall(pkgId);
}

async function checkGalleryPreflight() {
  const botId = document.getElementById('gd-preflight-bot')?.value;
  if (!botId) return;
  const pkgId = _galleryDetailsPkgId;
  if (!pkgId) return;

  const resultEl = document.getElementById('gd-preflight-results');
  resultEl.innerHTML = '<div style="font-size:0.8rem;color:var(--text3)">Checking…</div>';
  try {
    const pf = await api('GET', `/api/gallery/${pkgId}/preflight?bot=${encodeURIComponent(botId)}`);
    resultEl.innerHTML = _renderPreflightResults(pf);
  } catch(e) {
    resultEl.innerHTML = `<div style="color:var(--red);font-size:0.8rem">Error: ${escHtml(e.message)}</div>`;
  }
}

function _renderPreflightResults(pf) {
  if (pf.error) return `<div style="color:var(--red);font-size:0.8rem">${escHtml(pf.error)}</div>`;

  const readyColor = pf.ready_to_run ? 'var(--green)' : (pf.ready_to_build ? 'var(--yellow)' : 'var(--red)');
  const readyIcon  = pf.ready_to_run ? '✓' : (pf.ready_to_build ? '⚠' : '✗');
  const readyText  = pf.ready_to_run
    ? 'All requirements satisfied'
    : (pf.ready_to_build ? 'Will build — some runtime requirements missing' : 'Build blocked — resolve issues below');

  let html = `<div style="font-size:0.82rem;font-weight:500;color:${readyColor};margin-bottom:8px">${readyIcon} ${escHtml(readyText)}</div>`;

  const TYPE_LABELS = {integration:'Integration', secret:'Secret', system:'System tool', python_package:'Python package', file:'File', messaging_channel:'Messaging channel'};

  const items = [
    ...(pf.app_dependencies || []).map(d => ({
      label: d.display_name || d.pkg_id,
      typeLabel: 'App dependency',
      state: d.state, severity: d.severity, message: d.message, required: d.required,
      guidance: null,
    })),
    ...(pf.requirements || []).map(r => ({
      label: r.id,
      typeLabel: TYPE_LABELS[r.type] || r.type,
      state: r.state, severity: r.severity, message: r.message, required: r.required,
      guidance: r.state !== 'satisfied' ? (_SETUP_GUIDANCE[r.id] || null) : null,
    })),
  ];

  if (!items.length) {
    return html + `<div style="font-size:0.78rem;color:var(--text3)">No dependencies or requirements for this app.</div>`;
  }

  html += items.map(item => {
    const isOk  = item.state === 'satisfied';
    // Optional+missing items render with the muted "info" color, NOT the
    // yellow "you have a problem" tone — they're an available enhancement,
    // not a gap. Required+missing stays yellow (runtime_warning) or red
    // (build_blocker).
    const isInfo = !isOk && item.severity === 'info';
    const icon  = isOk
      ? '✓'
      : (item.state === 'unknown' || item.state === 'installing'
        ? '⟳'
        : (isInfo ? '○' : '✗'));
    const color = isOk
      ? 'var(--green)'
      : (item.severity === 'build_blocker'
        ? 'var(--red)'
        : (isInfo ? 'var(--text3)' : 'var(--yellow)'));
    return `<div style="display:flex;align-items:flex-start;gap:8px;padding:6px 0;border-bottom:1px solid var(--border)">
      <span style="color:${color};font-weight:700;min-width:14px;margin-top:1px">${icon}</span>
      <div style="flex:1;min-width:0">
        <div style="font-size:0.82rem;font-weight:500">${escHtml(item.label)}</div>
        <div style="font-size:0.72rem;color:var(--text3)">${escHtml(item.typeLabel)}${item.required ? '' : ' (optional)'}</div>
        <div style="font-size:0.78rem;color:${isOk ? 'var(--text3)' : 'var(--text2)'};margin-top:2px">${escHtml(item.message)}</div>
        ${item.guidance ? `<pre style="font-size:0.72rem;background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:6px;margin-top:6px;white-space:pre-wrap;color:var(--text2);overflow-x:auto">${escHtml(item.guidance)}</pre>` : ''}
      </div>
    </div>`;
  }).join('');

  return html;
}

// Preflight check inside the install modal — runs for all checked bots
async function runInstallPreflight() {
  const pkgId = _galleryInstallPkgId;
  if (!pkgId) return;

  const checked = Array.from(
    document.querySelectorAll('#gallery-install-bots input[name="install-bot"]:checked')
  ).map(el => el.value);

  const pfDiv  = document.getElementById('gallery-install-preflight');
  const pfBody = document.getElementById('gallery-install-preflight-body');
  const forceWrap = document.getElementById('gallery-install-force-wrap');

  if (!checked.length) {
    pfDiv.style.display = 'none';
    return;
  }

  pfDiv.style.display = 'block';
  pfBody.innerHTML = '<div style="font-size:0.8rem;color:var(--text3)">Checking…</div>';

  let anyBlocker = false;
  const parts = [];

  for (const botId of checked) {
    try {
      const pf = await api('GET', `/api/gallery/${pkgId}/preflight?bot=${encodeURIComponent(botId)}`);
      if (!pf.ready_to_build) anyBlocker = true;
      const statusIcon  = pf.ready_to_run ? '✓' : (pf.ready_to_build ? '⚠' : '✗');
      const statusColor = pf.ready_to_run ? 'var(--green)' : (pf.ready_to_build ? 'var(--yellow)' : 'var(--red)');
      // Filter to items that actually block: required-and-missing, plus
      // any state that prevents build regardless of required (e.g. an
      // app-dependency that's currently installing). Optional+missing items
      // are surfaced separately as "(optional — not configured)" so the
      // operator sees them without being warned they're blocking install.
      const blocking = [
        ...(pf.app_dependencies || []).filter(d => d.state !== 'satisfied' && d.required !== false),
        ...(pf.requirements || []).filter(r => r.state !== 'satisfied' && r.required !== false),
      ];
      const optional = [
        ...(pf.app_dependencies || []).filter(d => d.state !== 'satisfied' && d.required === false),
        ...(pf.requirements || []).filter(r => r.state !== 'satisfied' && r.required === false),
      ];
      const fmtBlock = x => `<span style="color:var(--text2);font-size:0.75rem">· ${escHtml((x.display_name || x.id || x.label || ''))}: ${escHtml(x.message)}</span>`;
      const fmtOpt   = x => `<span style="color:var(--text3);font-size:0.75rem">· ${escHtml((x.display_name || x.id || x.label || ''))} (optional — not configured)</span>`;
      let detail;
      if (blocking.length) {
        detail = blocking.map(fmtBlock).join('<br>');
        if (optional.length) detail += '<br>' + optional.map(fmtOpt).join('<br>');
      } else if (optional.length) {
        detail = `<span style="font-size:0.75rem;color:var(--text3)">All required satisfied</span><br>` + optional.map(fmtOpt).join('<br>');
      } else {
        detail = `<span style="font-size:0.75rem;color:var(--text3)">All requirements satisfied</span>`;
      }
      parts.push(`<div style="margin-bottom:8px">
        <div style="font-size:0.82rem;font-weight:500;color:${statusColor}">${statusIcon} ${escHtml(botLabel(botId))}</div>
        <div style="padding-left:18px;margin-top:2px">${detail}</div>
      </div>`);
    } catch(e) {
      parts.push(`<div style="font-size:0.78rem;color:var(--red)">${escHtml(botLabel(botId))}: error — ${escHtml(e.message)}</div>`);
    }
  }

  pfBody.innerHTML = parts.join('');
  if (forceWrap) forceWrap.style.display = anyBlocker ? 'flex' : 'none';
}




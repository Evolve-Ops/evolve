// ════════════════════════════════════════════════════════════════════════
// Page: Skills
//
// "Add a skill" surface: pod-wide skill matrix + browsable catalog with
// per-bot install/uninstall toggles. The page was reduced to a single
// Browse view post-V2.2-3 (per-bot detail moved to Plugins → Plugins).
//
// State:
//   _skillsPodData     — last pod-matrix response (skills × bots)
//   _skillsCatalogData — last catalog response { skills: [...] }
//
// Functions dispatched via onPageActivate('skills'):
//   skillsPageActivate()        — entry; defaults to Browse view
//   skillsShowView(view)        — tab switcher
//   skillsLoadPod(force)        — pod-matrix fetcher
//   _skillsRenderPod()          — pod-matrix grid renderer
//   skillsLoadCatalog(force)    — catalog fetcher
//   _skillsRenderCatalog()      — catalog list renderer
//   _skillsStatusBadge()        — status pill formatter
//   _skillsInstalledCellBadge() — per-cell status badge
//   _skillsCellClick()          — install/uninstall click handler
//   _skillsCatalogIdToAuditId() — catalog id → trail audit id mapping
//   Plus the audit-trail viewer that opens the per-cell history.
//
// Cross-file linkages (runtime free-variable lookup):
//   - api(), toast(), escHtml(), botLabel() — core/
//   - openCustomModal — still inline in main script (used by audit-trail)
//
// Out of scope (separate clusters):
//   loadPageSummary_skills (~line 11154) — Overview summary tile, used
//     across the Overview render path, not the Skills page itself.
//   _ikAuditSkillsForPlugin (~line 8652) — Plugins page helper.
//   _wizGotoSkillsPage (~line 16288) — wizard handoff helper.
// ════════════════════════════════════════════════════════════════════════

// Skills page
// ══════════════════════════════════════════════════════
// State
let _skillsPodData     = null; // pod matrix response
let _skillsCatalogData = null; // catalog response { skills: [...] }

function skillsPageActivate() {
  // Default tab is "Add a skill" — it's the action-oriented view and the
  // only one that surfaces skills the user hasn't installed yet. Per-bot
  // was removed (Plugins → Plugins covers that ground more usefully).
  skillsShowView('catalog');
  skillsLoadCatalog();
}

function skillsShowView(view) {
  const podView     = document.getElementById('skills-view-pod');
  const catalogView = document.getElementById('skills-view-catalog');
  const podTab      = document.getElementById('skills-tab-pod');
  const catalogTab  = document.getElementById('skills-tab-catalog');
  if (!podView || !catalogView) return;

  podView.style.display     = view === 'pod'     ? '' : 'none';
  catalogView.style.display = view === 'catalog' ? '' : 'none';
  podTab?.classList.toggle('active',     view === 'pod');
  catalogTab?.classList.toggle('active', view === 'catalog');

  // Load/render each view when switched to. skillsLoadPod() and
  // skillsLoadCatalog() each have internal caching — if data is already
  // in _skillsPodData / _skillsCatalogData they skip the fetch and render
  // immediately. We must call them unconditionally rather than guarding on
  // !_skillsPodData here, because skillsLoadCatalog() pre-populates
  // _skillsPodData as a side-effect — causing the pod view to skip its
  // render and stay stuck on the static "Loading…" spinner.
  if (view === 'pod')     skillsLoadPod();
  if (view === 'catalog') skillsLoadCatalog();
}

function _skillsStatusBadge(status) {
  // Friendly labels — no jargon for the Plex-test crowd. Shared by the
  // Across-pod matrix today; the dropped Per-bot tab also used these.
  const map = {
    configured:     { cls: 'badge-ok',   label: 'Configured' },
    needs_oauth:    { cls: 'badge-warn', label: 'Needs setup' },
    missing_config: { cls: 'badge-gray', label: 'Not configured' },
    error:          { cls: 'badge-red',  label: 'Error' },
  };
  const { cls, label } = map[status] || { cls: 'badge-member', label: escHtml(status || '?') };
  return `<span class="badge ${cls}">${label}</span>`;
}

// Augmented version of the cell badge used in Skills → Installed. Overlays
// the latest audit-health on top of the inventory state for the
// (skill_id, bot_id) tuple. Healthy and never-audited render identically
// (absence of audit isn't a problem); findings + audit-failed darken /
// recolour the badge with a small glyph + hover tooltip carrying the
// finding count. Not-configured / needs-setup pass through unchanged —
// audit only applies to installed skills.
function _skillsInstalledCellBadge(status, botId, skillId) {
  // Resolve the audit_id the substrate auditor knows about. The catalog
  // skill_id (`obsidian_vault`, `gog`, ...) doesn't always match KNOWN_SKILLS
  // (`obsidian`, ...) — bridge via the same plugin-name mapping the Plugins
  // tab uses, in reverse.
  const map = {
    configured:     { cls: 'badge-ok',   label: 'Configured' },
    needs_oauth:    { cls: 'badge-warn', label: 'Needs setup' },
    missing_config: { cls: 'badge-gray', label: 'Not configured' },
    error:          { cls: 'badge-red',  label: 'Error' },
  };
  const base = map[status];
  if (!base || status !== 'configured') {
    // Non-configured states get the plain badge — audit doesn't apply.
    return _skillsStatusBadge(status);
  }
  // Look up audit state. Resolve catalog id → audit id (best-effort).
  const auditId = _skillsCatalogIdToAuditId(skillId);
  const info = auditId
    ? (_substrateAuditStatus.skill?.[botId] || {})[auditId]
    : null;
  let cls = 'badge-ok', label = 'Configured', tip = '';
  if (info && info.status === 'findings') {
    cls = 'badge-warn';
    const n = info.raised_count || 0;
    label = `Configured ⚠`;
    tip = `${n} audit finding${n === 1 ? '' : 's'} on ${botLabel(botId)}. Click for details.`;
  } else if (info && info.status === 'failed') {
    cls = 'badge-red';
    label = `Configured ❌`;
    tip = `Last audit run failed on ${botLabel(botId)}. Click for details.`;
  } else {
    // healthy / never / no audit data → plain Configured (absence of audit
    // is not a failure, per spec).
    tip = 'Configured. Click to manage on the bot\'s Plugins tab.';
  }
  // Cells deep-link to the per-bot Plugins page so the operator can
  // drill in to run an audit / inspect the trail.
  return `<a href="#" onclick="_skillsCellClick('${escHtml(botId)}','${escHtml(skillId)}'); return false" title="${escHtml(tip)}" style="text-decoration:none"><span class="badge ${cls}">${label}</span></a>`;
}

// Catalog skill_id → KNOWN_SKILLS audit id. Defaults to the catalog id
// when no remap is needed (slack, telegram, etc).
function _skillsCatalogIdToAuditId(catalogId) {
  const remap = {
    obsidian_vault: 'obsidian',
    gog:    'gmail',  // gog bundle is gated on gmail audit health
    gmail:  'gmail',
    calendar: 'calendar',
    gdrive: 'upstream_plugin_skills',
    brave:  'upstream_plugin_skills',
    github: 'upstream_plugin_skills',
    dropbox: 'upstream_plugin_skills',
    unity:  'upstream_plugin_skills',
  };
  return remap[catalogId] || catalogId;
}

// Cell deep-link from Skills → Installed to the per-bot Plugins page.
function _skillsCellClick(botId, skillId) {
  // Switch to the Plugins page and select the bot.
  const nav = document.querySelector('.nav-item[data-page="integrations-keys"]');
  if (nav) nav.click();
  // Defer the bot-switch until ikRenderKeys has had a turn.
  setTimeout(() => {
    if (typeof ikSwitchBot === 'function') ikSwitchBot(botId);
    // Scroll to plugins subtab and try to land on the row.
    const sub = document.querySelector('#page-integrations-keys .subtab[onclick*="plugins"]');
    if (sub) sub.click();
  }, 50);
}

async function skillsLoadPod(forceRefresh) {
  const content = document.getElementById('skills-pod-content');
  if (!content) return;

  if (forceRefresh) _skillsPodData = null;

  if (!_skillsPodData) {
    content.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
    const d = await api('GET', '/api/skills/pod');
    if (!d || d.error) {
      content.innerHTML = `<div class="empty" style="color:var(--red)">
        Could not load pod skills: ${escHtml(d?.error || 'Unknown error')}
      </div>`;
      return;
    }
    _skillsPodData = d;
  }

  // Audit-health overlay on the installed-cell badges. Best-effort; if
  // the call fails the cells render with the plain status badge.
  await _loadSubstrateAuditStatusForAllBots('skill');

  _skillsRenderPod(_skillsPodData, content);
}

function _skillsRenderPod(d, content) {
  const allBotIds = d.all_bot_ids || [];
  const matrix    = d.matrix    || {};
  const skillMeta = d.skill_meta || {};
  const botData   = d.bots       || {};

  if (!allBotIds.length) {
    content.innerHTML = '<div class="empty">No bots found in pod.</div>';
    return;
  }

  const skillIds = Object.keys(matrix);
  if (!skillIds.length) {
    content.innerHTML = '<div class="empty">No skills found across the pod.</div>';
    return;
  }

  // Detect bots with read errors for tooltip
  const botErrors = {};
  for (const [bid, inv] of Object.entries(botData)) {
    if (inv && inv.read_error) botErrors[bid] = inv.read_error;
  }

  // Table: rows = skills, columns = bots
  const colWidth = `${Math.max(80, Math.floor(500 / allBotIds.length))}px`;

  let html = `<div style="overflow-x:auto">
    <table style="border-collapse:collapse;width:100%;min-width:${allBotIds.length * 90 + 200}px;font-size:0.8rem">
      <thead>
        <tr>
          <th style="text-align:left;padding:6px 10px;border-bottom:1px solid var(--border);color:var(--text3);font-weight:600;font-size:0.72rem;min-width:160px">Skill</th>`;

  for (const bid of allBotIds) {
    const errTitle = botErrors[bid] ? ` title="${escHtml(botErrors[bid])}"` : '';
    const errStyle = botErrors[bid] ? 'color:var(--yellow)' : '';
    html += `<th style="text-align:center;padding:6px 8px;border-bottom:1px solid var(--border);
                        color:var(--text3);font-weight:600;font-size:0.72rem;width:${colWidth}"
               ${errTitle}>
               <span style="${errStyle}">${escHtml(botLabel(bid))}</span>
             </th>`;
  }

  html += `</tr></thead><tbody>`;

  for (const sid of skillIds) {
    const meta = skillMeta[sid] || {};
    const rowData = matrix[sid] || {};
    html += `<tr style="border-bottom:1px solid var(--border)">
      <td style="padding:7px 10px;font-weight:500">${escHtml(meta.display || sid)}</td>`;

    for (const bid of allBotIds) {
      const status = rowData[bid];
      if (status === null || status === undefined) {
        // Bot had a read error or skill absent
        const cell = botErrors[bid]
          ? `<span style="cursor:default;color:var(--text3)" title="${escHtml(botErrors[bid])}">?</span>`
          : `<span style="color:var(--text3)">—</span>`;
        html += `<td style="text-align:center;padding:7px 8px">${cell}</td>`;
      } else {
        html += `<td style="text-align:center;padding:7px 8px">${_skillsInstalledCellBadge(status, bid, sid)}</td>`;
      }
    }

    html += `</tr>`;
  }

  html += `</tbody></table></div>`;
  content.innerHTML = html;
}

async function skillsLoadCatalog(forceRefresh) {
  const content = document.getElementById('skills-catalog-content');
  if (!content) return;

  if (forceRefresh) {
    _skillsCatalogData = null;
    _skillsPodData = null;  // refresh per-bot install state too
  }

  if (!_skillsCatalogData) {
    content.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
    const d = await api('GET', '/api/skills/catalog');
    if (!d || d.error) {
      content.innerHTML = `<div class="empty" style="color:var(--red)">
        Could not load catalog: ${escHtml(d?.error || 'Unknown error')}
      </div>`;
      return;
    }
    _skillsCatalogData = d;
  }

  // Fetch the pod matrix in parallel so we can show which bots already have each skill.
  // Audit-status fetch lives on the per-bot management surface (Plugins page
  // and Skills → Installed), not on Browse — Browse is pure discovery.
  if (!_skillsPodData) {
    try { _skillsPodData = await api('GET', '/api/skills/pod'); } catch (_) { _skillsPodData = null; }
  }

  _skillsRenderCatalog(_skillsCatalogData, _skillsPodData, content);
}

// Catalog skill ids don't always match the openclaw plugin name in the
// inventory matrix. The matrix is keyed by the actual plugin/MCP/filesystem
// id detected in openclaw.json:
//   - gog        skill ⇄ plugins.entries.google           (inventory id: google)
//   - gdrive     skill ⇄ plugins.entries.google           (inventory id: google)
//   - gmail      skill ⇄ plugins.entries.google           (inventory id: google)
//   - calendar   skill ⇄ plugins.entries.google           (inventory id: google)
// Other skill ids (slack, telegram, discord, brave, github, dropbox,
// home_assistant, notion, obsidian_vault) match the inventory key directly.
const _SKILL_ID_TO_INVENTORY_KEYS = {
  gog:      ['google'],
  gdrive:   ['google'],
  gmail:    ['google'],
  calendar: ['google'],
};

// Brand icons for the catalog cards. We pull SVGs from cdn.simpleicons.org
// using each brand's official hex; the CDN serves a single-color recolour of
// the canonical mark. Slack pulled their logo from Simple Icons in 2024, so
// we let it fall back to the letter-circle path below. Same fallback covers
// any future 404 if a brand revokes permission.
const _SKILL_ICONS = {
  // Unified ``google`` skill (PR #2231) — uses the canonical Google
  // multicolor logo. The legacy gog/gmail/calendar/gdrive entries below
  // are kept for migration deep-links but are no longer surfaced in the
  // catalog list.
  google:         { slug: 'google',        color: '4285F4' },
  gog:            { slug: 'gmail',         color: 'EA4335' },
  gdrive:         { slug: 'googledrive',   color: '4285F4' },
  gmail:          { slug: 'gmail',         color: 'EA4335' },
  calendar:       { slug: 'googlecalendar', color: '4285F4' },
  discord:        { slug: 'discord',       color: '5865F2' },
  telegram:       { slug: 'telegram',      color: '26A5E4' },
  obsidian_vault: { slug: 'obsidian',      color: '7C3AED' },
  brave:          { slug: 'brave',         color: 'FB542B' },
  github:         { slug: 'github',        color: '181717' },
  dropbox:        { slug: 'dropbox',       color: '0061FF' },
  home_assistant: { slug: 'homeassistant', color: '18BCF2' },
  notion:         { slug: 'notion',        color: '000000' },
  apple_local:    { slug: 'apple',         color: 'A2AAAD' },
  imessage:       { slug: 'imessage',      color: '34DA50' },
  // WhatsApp brand green (25D366). Added with the WhatsApp install module
  // (PR #2130). Simple Icons carries the official mark.
  whatsapp:       { slug: 'whatsapp',      color: '25D366' },
  // Signal brand blue (3A76F0). Added with the Signal install module
  // (Phase 1.3). LICENSING REVIEW REQUIRED BEFORE MERGE — see
  // signal_install module docstring. The logo entry is harmless on its
  // own; if the review withdraws the skill, this row stays inert
  // (no catalog → no card → no icon lookup).
  signal:         { slug: 'signal',        color: '3A76F0' },
  linear:         { slug: 'linear',        color: '5E6AD2' },
  unity:          { slug: 'unity',         color: '000000' },
  autocad:        { slug: 'autocad',       color: 'E51050' },
  // runway: no simpleicons entry — letter-circle fallback below.
  // slack: no simpleicons entry (revoked in 2024) → letter-circle fallback,
  // see _renderSkillIcon below. Keep the fallback colour close to Slack's
  // Aubergine so the brand still reads at a glance.
};

const _SKILL_FALLBACK_COLORS = {
  slack: '4A154B',
  // Runway brand reads as neutral/black; the letter-R circle in black
  // matches the look of their dashboard.
  runway: '000000',
};

function _renderSkillIcon(skillId, displayName) {
  const size = 36;
  const spec = _SKILL_ICONS[skillId];
  const letter = (displayName || skillId || '?').trim().charAt(0).toUpperCase();
  const fallbackColor = _SKILL_FALLBACK_COLORS[skillId] || '6366F1';
  const fallbackHtml =
    `<div style="width:${size}px;height:${size}px;flex-shrink:0;`
      + `border-radius:8px;background:#${fallbackColor};display:flex;`
      + `align-items:center;justify-content:center;color:white;`
      + `font-weight:700;font-size:0.95rem">${escHtml(letter)}</div>`;

  if (!spec || !spec.slug) return fallbackHtml;

  // Brands with near-black canonical colors are invisible on dark backgrounds.
  // Flip them to white when the UI is in dark mode.
  const _darkOverrides = { github: 'F0F6FF', notion: 'FFFFFF', unity: 'FFFFFF' };
  const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
  const iconColor = (isDark && _darkOverrides[spec.slug]) || spec.color;
  const src = `https://cdn.simpleicons.org/${encodeURIComponent(spec.slug)}/${encodeURIComponent(iconColor)}`;
  // onerror swaps the failed <img> for the letter circle in place so a
  // single broken slug doesn't leave a torn-image glyph in the card.
  return `<img src="${src}" alt="${escHtml(displayName || skillId)}"
    width="${size}" height="${size}"
    style="flex-shrink:0;border-radius:8px;padding:4px;background:var(--bg2);box-sizing:border-box"
    onerror="this.outerHTML=${JSON.stringify(fallbackHtml).replace(/"/g, '&quot;')}" />`;
}

function _bestStatusForSkill(skillId, matrix, botId) {
  const candidates = [skillId, ...(_SKILL_ID_TO_INVENTORY_KEYS[skillId] || [])];
  for (const key of candidates) {
    const row = matrix[key];
    if (row && row[botId]) return row[botId];
  }
  return null;
}

// Generic skill-uninstall handler. Wired from the "×" affordance next to
// the per-bot install button on the Skills catalog card. Calls the
// existing per-skill /api/skills/install/<id>/revoke route — those exist
// for all twelve catalog skills today (telegram, slack, discord, obsidian,
// dropbox, notion, github, runway, linear, imessage, whatsapp, signal).
//
// Soft uninstall: backend clears channels.<id> / plugins.entries.<id> /
// keystore slot / marker file and kickstarts the gateway. Auth dirs are
// preserved so re-install can resume from the existing session.
async function _skillUninstallClick(skillId, skillName, botId, botLabel) {
  const ok = await confirmModal({body: (
    `Disconnect ${skillName} from ${botLabel}?\n\n` +
    `This clears the local connection. Re-installing later will need a fresh setup.`
  ), danger: true});
  if (!ok) return;

  try {
    const r = await api('POST', `/api/skills/install/${encodeURIComponent(skillId)}/revoke`, {
      bot_id: botId,
    });
    if (r && (r.ok === true || r.cleared === true)) {
      toast(`${skillName} disconnected from ${botLabel}`, 'ok');
      // Refresh the catalog matrix so the card re-renders with the
      // post-revoke status (typically null → "+ Add" button).
      if (typeof skillsPageActivate === 'function') skillsPageActivate();
      return;
    }
    const detail = (r && (r.detail || r.error)) || 'unknown error';
    toast(`Failed to uninstall ${skillName}: ${detail}`, 'error');
  } catch (e) {
    toast(`Failed to uninstall ${skillName}: ${e?.message || e}`, 'error');
  }
}

function _skillsRenderCatalog(catalog, pod, content) {
  const skills = (catalog && catalog.skills) || [];
  if (!skills.length) {
    content.innerHTML = '<div class="empty">No skills in the catalog.</div>';
    return;
  }

  const bots = Object.keys(_networkData.bots || {});
  if (!bots.length) {
    content.innerHTML = '<div class="empty">No bots in this pod — add a bot first.</div>';
    return;
  }

  const matrix = (pod && pod.matrix) || {};

  let html = `<div style="font-size:0.78rem;color:var(--text3);margin-bottom:12px">
    Pick a skill and add it to a bot. The setup wizard will walk through any required
    accounts or tokens.
  </div>`;

  // Categorical grouping (2026-06-04 catalog-UX polish). Server emits
  // category + category_order on each skill plus a category_order array.
  // We render section headers in that order and group cards under each.
  // The server also alpha-sorts within category, so iterating the flat
  // skills array preserves both groupings.
  const categoryOrder = (catalog && catalog.category_order)
    || ["Messaging", "Productivity", "Storage", "Tools", "Creative"];
  // Bucket skills by category; preserve server-supplied alpha order within.
  const byCategory = {};
  for (const s of skills) {
    const cat = s.category || "Other";
    if (!byCategory[cat]) byCategory[cat] = [];
    byCategory[cat].push(s);
  }
  // Tail-section for any "Other" category not in category_order
  const allCategories = [...categoryOrder];
  for (const cat of Object.keys(byCategory)) {
    if (!allCategories.includes(cat)) allCategories.push(cat);
  }

  for (const category of allCategories) {
    const inCat = byCategory[category];
    if (!inCat || !inCat.length) continue;

    html += `<h3 style="font-size:0.78rem;text-transform:uppercase;
      letter-spacing:0.06em;color:var(--text3);font-weight:600;
      margin:18px 0 8px 0">${escHtml(category)}</h3>`;
    html += `<div style="display:flex;flex-direction:column;gap:12px">`;

  for (const s of inCat) {
    const skillId = s.id;

    let botBtns = '';
    for (const b of bots) {
      // Per-bot capability summary (post-PR-#2231 + #2234 + #2239).
      // The backend returns capability_summaries.<skill_id>.<bot_id> as
      // either undefined OR a dict {summary, labels}. Pre-#2239 shape
      // was a bare string; the conditional below tolerates both so a
      // stale browser session against an older backend keeps working.
      const summaries = (pod && pod.capability_summaries) || {};
      const summaryEntry = (summaries[skillId] || {})[b];
      let capSummary = null;
      let capLabels = [];
      if (typeof summaryEntry === 'string') {
        capSummary = summaryEntry;             // legacy shape
      } else if (summaryEntry && typeof summaryEntry === 'object') {
        capSummary = summaryEntry.summary || null;
        capLabels = Array.isArray(summaryEntry.labels) ? summaryEntry.labels : [];
      }

      let status, installed, needsSetup;
      if (skillId === 'google') {
        // The unified ``google`` skill DOES NOT use the inventory matrix
        // for installed-state because matrix['google'] is OC's bundled
        // ``@openclaw/google-plugin`` (the Gemini LLM provider) — a
        // different thing entirely. Bots with Gemini enabled would
        // false-positive as "✓ installed" under Google Workspace.
        // capability_summaries is the authoritative source: the resolver
        // populates it for a bot configured for Workspace by EITHER path —
        // Path A (OAuth profile with recognised capabilities) or Path C
        // (service_account_dwd, scopes from network.json::google_integration).
        // Both yield a scope-derived {summary, labels}; an unconfigured bot
        // gets nothing and falls through to "+ Add".
        installed = !!capSummary;
        status = installed ? 'active' : null;
        needsSetup = false;
      } else {
        status = _bestStatusForSkill(skillId, matrix, b);
        installed = status === 'configured' || status === 'active' || status === 'valid';
        // needs_tcc (apple_local — some Apple-app permissions still ungranted)
        // is treated as "Finish setup" alongside the OAuth not-yet-completed
        // states so the user gets the same "you started; tap to keep going" UX.
        needsSetup = status === 'needs_oauth' || status === 'missing_config'
                  || status === 'needs_tcc';
      }
      // Use the display name (botLabel) for the button text; the
      // value passed to openSkillInstall stays as the raw bot id so
      // the backend route still resolves correctly.
      const bLabel = botLabel(b);
      let label, btnCls, title;
      if (installed) {
        label = capSummary ? `✓ ${bLabel} (${capSummary})` : `✓ ${bLabel}`;
        btnCls = 'btn btn-ghost btn-sm';
        // Tooltip: enumerate labels when summary is "custom" (the bare
        // word doesn't tell the operator what they have). Falls back to
        // the summary string otherwise.
        if (capSummary === 'custom' && capLabels.length > 0) {
          title = `Installed: ${capLabels.join(', ')}. Click to modify.`;
        } else if (capSummary) {
          title = `Installed: ${capSummary}. Click to modify capabilities.`;
        } else {
          title = 'Already installed — click to review or reconfigure';
        }
      } else if (needsSetup) {
        label = `Finish setup: ${bLabel}`;
        btnCls = 'btn btn-secondary btn-sm';
        title = 'Started but not finished — click to complete';
      } else {
        label = `+ Add to ${bLabel}`;
        btnCls = 'btn btn-primary btn-sm';
        title = 'Install this skill on this bot';
      }
      // Skills page Browse → "+ Add to <bot>" / "Finish setup" buttons.
      // Goes through _openSkillInstallOrUnifiedGoogle so Google skill IDs
      // (gog/gmail/calendar) divert to the unified Google chooser modal,
      // and every other skill keeps the existing openSkillInstall behavior
      // unchanged. The divert exists because Google has two account-type
      // paths (Workspace + DwD vs Personal OAuth) that need a chooser
      // step; the old direct-to-openSkillInstall flow dropped operators
      // into the read-only Personal path with no way to land in Workspace.
      const primaryBtn = `<button class="${btnCls}" title="${escHtml(title)}"
        onclick="_openSkillInstallOrUnifiedGoogle('${escHtml(skillId)}', '${escHtml(b)}')">${escHtml(label)}</button>`;

      // Uninstall affordance — visible whenever the skill has any
      // residue on this bot (status !== null), i.e. for `installed` AND
      // `needsSetup` states. Surfaces a soft-uninstall: POST to the
      // existing /api/skills/install/<id>/revoke route which clears
      // local config (channels.<id> + plugins.entries.<id> + keystore
      // slot where applicable) and kickstarts the gateway. Auth dirs
      // (Baileys / signal-cli state, OAuth refresh tokens) are
      // intentionally left on disk — re-install can resume from them.
      // Hidden for the "+ Add" state because there's nothing to undo
      // (and the button would just create operator confusion).
      let uninstallBtn = '';
      if (status !== null && status !== undefined) {
        const skName = s.display_name || skillId;
        const uninstallTitle = `Disconnect ${skName} from ${bLabel}`;
        uninstallBtn = `<button class="btn btn-ghost btn-sm"
          style="padding:4px 8px;color:var(--text3)"
          title="${escHtml(uninstallTitle)}"
          onclick="_skillUninstallClick('${escHtml(skillId)}', '${escHtml(skName)}', '${escHtml(b)}', '${escHtml(bLabel)}')">×</button>`;
      }

      botBtns += `<span style="display:inline-flex;align-items:center;gap:2px">${primaryBtn}${uninstallBtn}</span>`;
    }

    // Browse is pure discovery — install/add buttons only. Per-skill audit
    // affordances (status pill, Run audit, cadence) live on the per-bot
    // Plugins page where the operator manages an installed skill. Skills →
    // Installed gives an at-a-glance pod-wide audit-health overlay.

    const iconHtml = _renderSkillIcon(skillId, s.display_name);
    html += `<div class="card" style="padding:14px 18px;display:flex;gap:14px;align-items:flex-start">
      ${iconHtml}
      <div style="flex:1;min-width:0">
        <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:6px">
          <div style="font-size:0.95rem;font-weight:600">${escHtml(s.display_name || skillId)}</div>
          <div style="font-size:0.7rem;color:var(--text3);font-family:monospace">${escHtml(skillId)}</div>
        </div>
        <div style="font-size:0.82rem;color:var(--text2);margin-bottom:10px">${escHtml(s.summary || '')}</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">${botBtns}</div>
      </div>
    </div>`;
  }

    // close the per-category card-stack <div>
    html += `</div>`;
  }
  content.innerHTML = html;
}

// ── Substrate audit chips (Skills + OAuth providers) ──────────────────
// Closes the deferred UI affordances from PR #1218 (audit-extensions
// spec §4.1 + §4.2). Per-row affordances mirror the Applications-page
// chip pattern: status pill -> opens trail viewer modal, "Run audit now"
// button kicks the runner, cadence dropdown writes a per-bot override
// to network.json.

// Cache: {element_type: {bot_id: {element_id: {status, raised_count, ...}}}}.
const _substrateAuditStatus = { skill: {}, provider: {} };
const _substrateAuditCadence = { skill: {}, provider: {} };

async function _loadSubstrateAuditStatusForBot(elementType, botId) {
  if (!botId) return;
  try {
    const d = await api('GET', `/api/${elementType}s/${encodeURIComponent(botId)}/audit-status`);
    if (d && d.ok && d.elements) {
      _substrateAuditStatus[elementType][botId] = d.elements;
    }
  } catch (_) { /* keep stale or empty */ }
  try {
    const c = await api('GET', `/api/${elementType}s/${encodeURIComponent(botId)}/audit-cadence`);
    if (c && c.ok) {
      _substrateAuditCadence[elementType][botId] = c;
    }
  } catch (_) { /* keep stale */ }
}

async function _loadSubstrateAuditStatusForAllBots(elementType) {
  const bots = Object.keys(_networkData?.bots || {});
  await Promise.all(bots.map(b => _loadSubstrateAuditStatusForBot(elementType, b)));
}

function _substrateAuditPill(elementType, botId, elementId) {
  const info = (_substrateAuditStatus[elementType]?.[botId] || {})[elementId];
  if (!info || info.status === 'never') {
    return `<span class="badge badge-gray" title="No audit has run yet for this ${elementType} on ${escHtml(botLabel(botId))}.">– never audited</span>`;
  }
  if (info.status === 'failed') {
    const err = info.error ? ` — ${escHtml(String(info.error).slice(0, 60))}` : '';
    return `<span class="badge badge-red" title="Last audit run errored.${err}">❌ audit failed</span>`;
  }
  if (info.status === 'findings') {
    const n = info.raised_count || 0;
    return `<span class="badge badge-warn" title="${n} finding${n===1?'':'s'} raised; click for trail">⚠ ${n} finding${n===1?'':'s'}</span>`;
  }
  return `<span class="badge badge-ok" title="Last audit was clean.">✓ healthy</span>`;
}

function _renderSubstrateAuditRows(elementType, elementId, botIds) {
  if (!botIds || !botIds.length) return '';
  const cadenceOpts = [
    { v: '',          l: 'inherit pod default' },
    { v: 'never',     l: 'never (manual only)' },
    { v: 'daily',     l: 'daily' },
    { v: 'weekly',    l: 'weekly' },
    { v: 'monthly',   l: 'monthly' },
    { v: 'quarterly', l: 'quarterly' },
  ];
  const rows = botIds.map(b => {
    const cad = _substrateAuditCadence[elementType]?.[b] || {};
    const sel = cad.bot_override || '';
    const optsHtml = cadenceOpts.map(o =>
      `<option value="${escHtml(o.v)}" ${sel === o.v ? 'selected' : ''}>${escHtml(o.l)}</option>`
    ).join('');
    const pill = _substrateAuditPill(elementType, b, elementId);
    const pillClickable = `<a href="#" onclick="_openSubstrateTrail('${escHtml(elementType)}', '${escHtml(b)}', '${escHtml(elementId)}'); return false" style="text-decoration:none">${pill}</a>`;
    return `<div style="display:flex;align-items:center;gap:8px;margin-top:6px;font-size:0.75rem;flex-wrap:wrap">
      <span style="color:var(--text3);min-width:60px">${escHtml(botLabel(b))}</span>
      ${pillClickable}
      <button class="btn btn-ghost btn-sm" style="font-size:0.72rem;padding:2px 8px" onclick="_runSubstrateAuditNow('${escHtml(elementType)}', '${escHtml(b)}', '${escHtml(elementId)}')">Run audit now</button>
      <select class="form-select input-w-auto" style="font-size:0.72rem;padding:2px 6px"
              title="Audit cadence for this ${escHtml(elementType)} on ${escHtml(botLabel(b))}"
              onchange="_setSubstrateAuditCadence('${escHtml(elementType)}', '${escHtml(b)}', this.value)">${optsHtml}</select>
    </div>`;
  }).join('');
  return `<div style="margin-top:8px;border-top:1px solid var(--border);padding-top:6px">
    <div style="font-size:0.68rem;color:var(--text3);text-transform:uppercase;letter-spacing:0.05em;font-weight:600;margin-bottom:2px">Audit status</div>
    ${rows}
  </div>`;
}

async function _runSubstrateAuditNow(elementType, botId, elementId) {
  try {
    const r = await api(
      'POST',
      `/api/${elementType}s/${encodeURIComponent(botId)}/${encodeURIComponent(elementId)}/audit`,
      { full_audit: false },
    );
    if (r.ok) {
      toast(`✓ Audit queued for ${elementId} on ${botLabel(botId)}`, 'ok');
      // Refresh status after a small delay; runner is async on the bot.
      setTimeout(() => _refreshSubstrateAuditPage(elementType), 1500);
    } else {
      toast('✗ Audit dispatch failed: ' + (r.error || '?'), 'err');
    }
  } catch (e) {
    toast('✗ Audit failed: ' + e, 'err');
  }
}

async function _setSubstrateAuditCadence(elementType, botId, cadence) {
  try {
    const r = await api(
      'PUT',
      `/api/${elementType}s/${encodeURIComponent(botId)}/audit-cadence`,
      { cadence },
    );
    if (r.ok) {
      const friendly = cadence || 'pod default';
      toast(`✓ Audit cadence on ${botLabel(botId)} set to ${friendly}`, 'ok');
      // Patch local cache so re-renders show the new value.
      _substrateAuditCadence[elementType][botId] = Object.assign(
        {}, _substrateAuditCadence[elementType][botId] || {},
        { bot_override: r.bot_override },
      );
    } else {
      toast('✗ Cadence update failed: ' + (r.error || '?'), 'err');
    }
  } catch (e) {
    toast('✗ Cadence update failed: ' + e, 'err');
  }
}

function _refreshSubstrateAuditPage(elementType) {
  // Reload the current Plugins / Skills-Installed view so the pills show
  // fresh state. The Plugins page (Plugins → Plugins) is the per-bot home
  // for skill audit chips; Skills → Installed shows the pod-wide overlay.
  if (elementType === 'skill') {
    _loadSubstrateAuditStatusForAllBots('skill').then(() => {
      if (typeof ikRenderPlugins === 'function' && _ikPluginsData) ikRenderPlugins();
      if (typeof _skillsPodData !== 'undefined' && _skillsPodData) {
        const podContent = document.getElementById('skills-pod-content');
        if (podContent) _skillsRenderPod(_skillsPodData, podContent);
      }
    });
  } else {
    _loadSubstrateAuditStatusForAllBots('provider').then(() => {
      if (typeof loadIntegrationsKeys === 'function') loadIntegrationsKeys();
    });
  }
}

async function _openSubstrateTrail(elementType, botId, elementId) {
  const esc = escHtml;
  try {
    const r = await api(
      'GET',
      `/api/${elementType}s/${encodeURIComponent(botId)}/${encodeURIComponent(elementId)}/audit/trail?limit=50`,
    );
    const entries = (r && r.entries) || [];
    let body;
    if (!entries.length) {
      body = `<div style="padding:24px;color:var(--text3);text-align:center">No audit trail yet for this ${esc(elementType)} on ${esc(botLabel(botId))}.</div>`;
    } else {
      body = '<table style="width:100%;border-collapse:collapse;font-size:0.8rem"><thead><tr style="text-align:left;color:var(--text2);border-bottom:1px solid var(--border)"><th style="padding:4px 8px">When</th><th style="padding:4px 8px">Kind</th><th style="padding:4px 8px">Detail</th></tr></thead><tbody>';
      for (const e of entries.slice().reverse()) {
        const ts = esc(e.ts || '?');
        const kind = esc(e.kind || '?');
        let detail = '';
        if (e.kind === 'audit_run') {
          detail = `${esc(e.status || '')} · ${e.findings_count || 0} findings`;
        } else if (e.severity || e.category) {
          detail = `${esc(e.severity || '')} · ${esc((e.summary || e.rationale || '').slice(0, 100))}`;
        } else {
          detail = esc(JSON.stringify(e).slice(0, 200));
        }
        body += `<tr style="border-bottom:1px solid var(--border3)"><td style="padding:4px 8px;color:var(--text3);font-family:monospace">${ts}</td><td style="padding:4px 8px">${kind}</td><td style="padding:4px 8px">${detail}</td></tr>`;
      }
      body += '</tbody></table>';
    }
    const wrap = `<div style="padding:16px"><h3 style="margin:0 0 12px 0;font-size:1rem">Audit trail — ${esc(elementId)} on ${esc(botLabel(botId))}</h3>${body}</div>`;
    if (typeof openCustomModal === 'function') {
      openCustomModal(wrap);
    } else {
      const win = window.open('', '_blank');
      if (win) win.document.write('<title>Audit trail</title>' + wrap);
      else toast('Trail loaded (' + entries.length + ' entries) — popups blocked', 'warn');
    }
  } catch (e) {
    toast('✗ Trail load failed: ' + e, 'err');
  }
}

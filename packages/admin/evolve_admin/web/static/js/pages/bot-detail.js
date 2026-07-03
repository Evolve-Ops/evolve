// ════════════════════════════════════════════════════════════════════════
// Bot detail card cluster
//
// Three related surfaces that share state across the SPA:
//
//   1. Display-name helpers (utility tier) — botLabel + orderedBotIds.
//      Called ~hundreds of times across every page; they read
//      _networkData (still inline in the main script) via script-scope
//      free-variable lookup. Could go in core/ in a future cleanup;
//      living with the Bot detail card for now because their first
//      callers were the handover + setup-checklist cards.
//
//   2. Handover card (per-bot handover modal):
//      renderBotHandoverCard + openHandoverModal + closeHandoverModal +
//      generateHandover + _renderHandoverResult + _copyHandoverLink.
//      Builds a Markdown-formatted summary the operator can paste into
//      Slack/Telegram/email when transferring bot ownership.
//
//   3. Setup checklist card (per-bot checklist):
//      renderBotSetupChecklistCard + _setupChecklistRender +
//      _setupChecklistStatusEl + _setupChecklistRow + _setupChecklistGo +
//      _setupChecklistSetState + openSetupChecklistModal +
//      closeSetupChecklistModal + _setupChecklistTileToggle.
//      Drives the per-bot "what's left to wire up" tile on the Bot
//      detail surface + Settings → Bots tile.
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - api(), toast(), escHtml() — core/
//   - nav() / window.nav — core/router.js (the setup-checklist Go button
//                          deep-links to the relevant config page)
//   - _networkData — main script (botLabel + orderedBotIds read it)
//   - loadStatus() (Overview) — refreshed after setup-checklist state changes
// ════════════════════════════════════════════════════════════════════════



// ══════════════════════════════════════════════════════
// Bot display-name helper
// ══════════════════════════════════════════════════════
// Returns the bot's display name (set via the rename feature, which
// wraps `openclaw agents set-identity`), falling back to the bot id
// when no display name has been set. Use this anywhere the UI
// surfaces a bot to the operator — tiles, dropdowns, lists.
function botLabel(botId) {
  if (!botId) return '';
  const bot = (_networkData && _networkData.bots && _networkData.bots[botId]);
  const dn = bot && bot.display_name;
  return (typeof dn === 'string' && dn.trim()) ? dn : botId;
}

// Canonical bot-list ordering: primary bot first, then others alphabetised by
// display label. Used by every bot tab bar, tile rail, and bot-card grid so
// the visible order matches across pages. Pass any `{botId: cfg}` shape —
// _statusData.bots, _networkData.bots, _auditData, _permissionsData.bots, etc.
//
// Primary resolution: status.primary → network.primary → role==='primary' →
// legacy "evolve" if present. Mirrors primary_bot_id() in
// packages/analyzer/primary_bot.py.
function orderedBotIds(botsObj) {
  if (!botsObj) return [];
  const ids = Object.keys(botsObj);
  if (!ids.length) return [];
  let primaryId = (_statusData && _statusData.primary)
    || (_networkData && _networkData.primary)
    || null;
  if (!primaryId || !ids.includes(primaryId)) {
    primaryId = null;
    for (const id of ids) {
      const b = botsObj[id];
      if (b && b.role === 'primary') { primaryId = id; break; }
    }
  }
  if (!primaryId && ids.includes('evolve')) primaryId = 'evolve';
  const rest = ids.filter(id => id !== primaryId).sort((a, b) =>
    botLabel(a).toLowerCase().localeCompare(botLabel(b).toLowerCase())
  );
  return primaryId ? [primaryId, ...rest] : rest;
}

// ══════════════════════════════════════════════════════
// Config → Bot → Handover card (V2.4-5)
// ══════════════════════════════════════════════════════
function renderBotHandoverCard(botId) {
  const el = document.getElementById('botcfg-handover');
  if (!el) return;
  if (!botId) {
    el.innerHTML = '<div class="empty">Select a bot above…</div>';
    return;
  }
  const safeBot = escHtml(botId);
  const display = escHtml(botLabel(botId));
  // PR K (2026-06-01): explanatory prose moved to the card's visible
  // subtitle in the HTML mount-point; render keeps only the action +
  // CLI alternative.
  el.innerHTML = `
    <div style="display:flex;gap:8px;align-items:end;flex-wrap:wrap">
      <button class="btn btn-primary btn-sm" onclick="openHandoverModal('${safeBot}')">Generate onboarding link…</button>
    </div>
    <div style="font-size:0.74rem;color:var(--text3);margin-top:10px">
      Or on the host: <code>sudo evolve-admin handover ${safeBot} -m "your message"</code>
    </div>
  `;
}

// Purpose anchor (Effectiveness Layer, Phase B): what this bot is FOR. The Fit
// Reviewer reads it to suggest ways the bot could help its owner more.
async function renderBotPurposeCard(botId) {
  const el = document.getElementById('botcfg-purpose');
  if (!el) return;
  if (!botId) { el.innerHTML = '<div class="empty">Select a bot above…</div>'; return; }
  el.innerHTML = '<div class="subtle" style="font-size:0.78rem">Loading…</div>';
  let data;
  try {
    data = await api('GET', `/api/bot/${encodeURIComponent(botId)}/purpose`);
  } catch (e) {
    el.innerHTML = `<div class="subtle" style="font-size:0.78rem;color:var(--red)">Couldn't load purpose.</div>`;
    return;
  }
  const cur = data.purpose || {};
  const opts = ['<option value="">— choose —</option>'].concat(
    (data.archetypes || []).map(a =>
      `<option value="${escHtml(a)}"${a === (cur.archetype || '') ? ' selected' : ''}>${escHtml(a)}</option>`)
  ).join('');
  el.innerHTML = `
    <div style="display:flex;gap:10px;align-items:end;flex-wrap:wrap">
      <label style="font-size:0.78rem;color:var(--text2)">Archetype<br>
        <select id="botpurpose-archetype" class="input-w-md">${opts}</select>
      </label>
      <label style="font-size:0.78rem;color:var(--text2)">Mission (one line)<br>
        <input id="botpurpose-mission" type="text" maxlength="280" class="input-w-lg"
               placeholder="e.g. Keep my schedule and triage email"
               value="${escHtml(cur.mission || '')}">
      </label>
      <button class="btn btn-primary btn-sm" onclick="saveBotPurpose('${escHtml(botId)}')">Save</button>
    </div>
    <div id="botpurpose-status" class="subtle" style="font-size:0.76rem;margin-top:8px;min-height:1.1em"></div>
  `;
}

async function saveBotPurpose(botId) {
  const archEl = document.getElementById('botpurpose-archetype');
  const missEl = document.getElementById('botpurpose-mission');
  const statusEl = document.getElementById('botpurpose-status');
  if (!archEl || !missEl) return;
  if (statusEl) statusEl.textContent = 'Saving…';
  try {
    const resp = await api('PUT', `/api/bot/${encodeURIComponent(botId)}/purpose`,
                           { archetype: archEl.value, mission: missEl.value });
    if (statusEl) statusEl.textContent = resp && resp.purpose ? 'Saved.' : 'Cleared.';
  } catch (e) {
    if (statusEl) statusEl.textContent = 'Save failed — try again.';
  }
}

async function openHandoverModal(botId) {
  // Build modal lazily — keeps the static HTML lean.
  const existing = document.getElementById('handover-modal');
  if (existing) existing.remove();
  const display = escHtml(botLabel(botId));
  const overlay = document.createElement('div');
  overlay.id = 'handover-modal';
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:9999;display:flex;align-items:center;justify-content:center;padding:24px';
  overlay.innerHTML = `
    <div style="background:var(--bg);color:var(--text);border-radius:10px;max-width:560px;width:100%;padding:24px;border:1px solid var(--border);max-height:90vh;overflow-y:auto">
      <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:12px">
        <div>
          <div style="font-size:1.1rem;font-weight:600">Hand off ${display}</div>
          <div style="font-size:0.82rem;color:var(--text2);margin-top:2px">Send the new user a one-tap onboarding link.</div>
        </div>
        <button class="btn btn-ghost btn-sm" onclick="closeHandoverModal()" style="font-size:1.2rem;line-height:1">×</button>
      </div>

      <div id="handover-modal-form">
        <div style="margin-bottom:14px">
          <label style="display:block;font-size:0.78rem;color:var(--text2);margin-bottom:5px">Custom greeting (optional)</label>
          <input type="text" id="handover-message" maxlength="240"
                 placeholder="e.g. Hi Diana — your assistant is ready, takes a minute to finish setting up."
                 style="width:100%;box-sizing:border-box;padding:8px 10px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:0.9rem">
          <div style="font-size:0.72rem;color:var(--text3);margin-top:4px">Shown on the landing page when the new user opens the link.</div>
        </div>

        <div style="margin-bottom:14px;display:flex;gap:14px;flex-wrap:wrap">
          <div>
            <label style="display:block;font-size:0.78rem;color:var(--text2);margin-bottom:5px">Expires in</label>
            <select id="handover-expires" class="form-select input-w-md"
                    style="font-size:0.85rem">
              <option value="3">3 days</option>
              <option value="7" selected>7 days</option>
              <option value="14">14 days</option>
              <option value="30">30 days</option>
            </select>
          </div>
          <div>
            <label style="display:block;font-size:0.78rem;color:var(--text2);margin-bottom:5px">For</label>
            <select id="handover-audience" class="form-select input-w-lg"
                    style="font-size:0.85rem">
              <option value="personal_bot_user" selected>Personal bot user (one owner)</option>
              <option value="team_bot_member">Team-bot member</option>
            </select>
          </div>
        </div>

        <div class="stripe-card is-warn" style="background:color-mix(in srgb, var(--yellow) 10%, var(--bg2));color:var(--text2);padding:10px 12px;border-radius:6px;font-size:0.78rem;margin-bottom:14px;line-height:1.5">
          <strong>The link is the authentication.</strong> Send it through a channel you trust (Signal, iMessage, SMS). You can pause this bot at any time with <code>evolve-admin pause-all</code> on the host.
        </div>

        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button class="btn btn-ghost btn-sm" onclick="closeHandoverModal()">Cancel</button>
          <button class="btn btn-primary btn-sm" id="handover-generate-btn" onclick="generateHandover('${escHtml(botId)}')">Generate link</button>
        </div>
      </div>

      <div id="handover-modal-result" style="display:none"></div>
    </div>
  `;
  document.body.appendChild(overlay);
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) closeHandoverModal();
  });
}

function closeHandoverModal() {
  const el = document.getElementById('handover-modal');
  if (el) el.remove();
}

async function generateHandover(botId, opts) {
  opts = opts || {};
  const btn = document.getElementById('handover-generate-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Generating…'; }
  const message = (document.getElementById('handover-message') || {}).value || '';
  const expires_in_days = parseInt((document.getElementById('handover-expires') || {}).value || '7', 10);
  const audience = (document.getElementById('handover-audience') || {}).value || 'personal_bot_user';
  try {
    const resp = await api('POST', '/api/handover/generate', {
      bot_id: botId,
      message,
      expires_in_days,
      audience,
      rotate: !!opts.rotate,
    });
    if (resp && resp.ok) {
      _renderHandoverResult(botId, resp);
    } else {
      toast((resp && resp.error) || 'Failed to generate handover link.', 'err');
      if (btn) { btn.disabled = false; btn.textContent = 'Generate link'; }
    }
  } catch (e) {
    toast('Network error: ' + String(e && e.message || e), 'err');
    if (btn) { btn.disabled = false; btn.textContent = 'Generate link'; }
  }
}

function _renderHandoverResult(botId, resp) {
  const form = document.getElementById('handover-modal-form');
  const result = document.getElementById('handover-modal-result');
  if (!form || !result) return;
  form.style.display = 'none';
  result.style.display = 'block';
  const safeUrl = escHtml(resp.url);
  const safeBot = escHtml(botId);
  const fresh = resp.created;
  const expires = resp.expires_at ? escHtml(resp.expires_at) : '—';
  const verb = fresh ? (resp.rotated ? 'New link (rotated)' : 'Link created') : 'Existing link';
  result.innerHTML = `
    <div style="margin-bottom:14px">
      <div style="font-size:0.82rem;color:var(--text2);margin-bottom:6px">${verb} for ${escHtml(botLabel(botId))}:</div>
      <div style="display:flex;gap:8px;align-items:stretch">
        <input id="handover-link-out" type="text" readonly value="${safeUrl}"
               style="flex:1;padding:9px 11px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:0.85rem;font-family:ui-monospace,monospace">
        <button class="btn btn-primary btn-sm" id="handover-copy-btn"
                onclick="_copyHandoverLink()">Copy</button>
      </div>
      <div style="font-size:0.74rem;color:var(--text3);margin-top:6px">Expires ${expires} · audience: ${escHtml(resp.audience || 'personal_bot_user')}</div>
    </div>

    <div style="background:var(--bg3);padding:10px 12px;border-radius:6px;font-size:0.78rem;color:var(--text2);line-height:1.5;margin-bottom:14px">
      <strong style="color:var(--text)">What happens next:</strong>
      <ol style="margin:6px 0 0 18px;padding:0">
        <li>Text this link to the new user (Signal, iMessage, SMS — any channel you trust).</li>
        <li>They tap it, pick voice and preferred name, then start chatting with the bot directly.</li>
        <li>The link expires automatically; you can pause the bot at any time with <code>evolve-admin pause-all</code>.</li>
      </ol>
    </div>

    <div style="display:flex;gap:8px;justify-content:space-between">
      <button class="btn btn-ghost btn-sm" onclick="generateHandover('${safeBot}',{rotate:true})">${fresh ? 'Rotate again' : 'Rotate (replace this link)'}</button>
      <button class="btn btn-primary btn-sm" onclick="closeHandoverModal()">Done</button>
    </div>
  `;
}

function _copyHandoverLink() {
  const input = document.getElementById('handover-link-out');
  const btn = document.getElementById('handover-copy-btn');
  if (!input) return;
  input.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (e) { ok = false; }
  if (!ok && navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(input.value).then(() => {
      if (btn) { btn.textContent = 'Copied'; setTimeout(() => btn.textContent = 'Copy', 1200); }
    });
    return;
  }
  if (btn) { btn.textContent = 'Copied'; setTimeout(() => btn.textContent = 'Copy', 1200); }
}

// ══════════════════════════════════════════════════════
// Config → Bot → Setup checklist card
// ══════════════════════════════════════════════════════
// Renders the per-bot Setup checklist (the permanent home referenced by
// the Overview tile's Setup chip and Actions ⋯ menu fallback). Each row
// shows a state badge + label + "Go" button + dismiss/un-dismiss toggle.
// Footer: counter and a "Stop showing on tile" / "Show on tile" toggle.
//
// Backend: routes_setup_checklist.py. Data layer: setup_checklist.py.
async function renderBotSetupChecklistCard(botId) {
  const el = document.getElementById('botcfg-setup-checklist');
  if (!el) return;
  if (!botId) {
    el.innerHTML = '<div class="empty">Select a bot above…</div>';
    return;
  }
  el.innerHTML = '<div class="subtle" style="font-size:0.82rem">Loading…</div>';
  let data;
  try {
    data = await api('GET', `/api/admin/bots/${encodeURIComponent(botId)}/setup-checklist`);
  } catch (e) {
    el.innerHTML = `<div class="empty">Failed to load: ${escHtml(String(e && e.message || e))}</div>`;
    return;
  }
  if (!data || data.error) {
    el.innerHTML = `<div class="empty">Failed to load: ${escHtml(data && data.error || 'unknown')}</div>`;
    return;
  }
  _setupChecklistRender(el, botId, data);
}

function _setupChecklistRender(el, botId, data) {
  const safeBot = escHtml(botId);
  const rows = (data.items || []).map(it => _setupChecklistRow(safeBot, it)).join('');
  const { done, total } = data.counter || { done: 0, total: 0 };
  const suppressed = !!(data.tile_chip && data.tile_chip.suppressed_at);
  const tileBtnLabel = suppressed ? 'Show on tile' : 'Stop showing on tile';
  const tileBtnAction = suppressed ? 'reset' : 'suppress';
  // Per-bot status div id so the Settings card and the Overview-tile
  // modal can both be mounted at once without document.getElementById
  // returning the wrong one (the Settings page is the typical second
  // mount; without scoping, status updates in the modal silently land
  // on the Settings card and look like nothing happened).
  const statusId = `setup-checklist-status-${safeBot}`;
  el.innerHTML = `
    <div>${rows}</div>
    <div style="margin-top:12px;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap">
      <div class="subtle" style="font-size:0.82rem">${done} of ${total} complete</div>
      <button class="btn btn-ghost btn-sm"
              onclick="_setupChecklistTileToggle('${safeBot}','${tileBtnAction}')"
              style="font-size:0.74rem;padding:4px 10px">${tileBtnLabel}</button>
    </div>
    <div id="${statusId}" class="subtle"
         style="margin-top:6px;font-size:0.76rem;min-height:1.1em"></div>
  `;
}

// Pick the right status div for messages. Both the Settings card and the
// Overview modal can be in the DOM at once; the render writes a bot-scoped
// id and the action handlers look up via that.
function _setupChecklistStatusEl(botId) {
  return document.getElementById(`setup-checklist-status-${botId}`);
}

function _setupChecklistRow(safeBot, item) {
  const state = item.state || 'pending';
  const label = escHtml(item.label || item.id);
  let badgeCls = 'badge';
  let badgeText = 'Pending';
  if (state === 'done') { badgeCls = 'badge badge-ok'; badgeText = 'Done'; }
  else if (state === 'dismissed') { badgeCls = 'badge badge-muted'; badgeText = 'Dismissed'; }
  const itemId = escHtml(item.id);
  // "Go" button — only useful when there's something to do (pending).
  // Done items hide it; dismissed items hide it (operator already said no).
  // Special case: the github row launches the per-bot mini-wizard
  // instead of just navigating to the integrations-keys page. The
  // wizard composes the four substeps (PAT → repo → wire backup →
  // enable MCP) in one focused flow.
  let goBtn = '';
  if (state === 'pending' && item.id === 'github') {
    goBtn = `<button class="btn btn-sm" onclick="openGithubSetupWizard('${safeBot}')"
                     style="font-size:0.74rem;padding:3px 10px">Set up ▸</button>`;
  } else if (state === 'pending' && item.deep_link) {
    goBtn = `<button class="btn btn-sm" onclick="_setupChecklistGo('${escHtml(item.deep_link)}','${safeBot}')"
                     style="font-size:0.74rem;padding:3px 10px">Go ▸</button>`;
  }
  // Dismiss / Un-dismiss toggle. Hidden for ``done`` items — there's
  // nothing to dismiss if the detector already says complete.
  let toggleBtn = '';
  if (state === 'pending') {
    toggleBtn = `<button class="btn btn-ghost btn-sm"
                         onclick="_setupChecklistSetState('${safeBot}','${itemId}','dismissed')"
                         style="font-size:0.72rem;padding:3px 8px;color:var(--text2)">Not for this bot</button>`;
  } else if (state === 'dismissed') {
    toggleBtn = `<button class="btn btn-ghost btn-sm"
                         onclick="_setupChecklistSetState('${safeBot}','${itemId}','pending')"
                         style="font-size:0.72rem;padding:3px 8px">Bring back</button>`;
  }
  return `
    <div style="display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--border);flex-wrap:wrap">
      <span class="${badgeCls}" style="min-width:74px;text-align:center;font-size:0.72rem">${badgeText}</span>
      <div style="flex:1;font-size:0.85rem">${label}</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">${goBtn}${toggleBtn}</div>
    </div>
  `;
}

// "Go" → click the matching nav-item. The deep_link is a data-page value
// (e.g. "users", "integrations-keys"). The destination page is responsible
// for its own per-bot selection; we don't try to forward the bot_id today.
// If the modal is open when Go fires, close it first so the nav transition
// looks clean instead of leaving an overlay floating above the new page.
function _setupChecklistGo(deepLink, botId) {
  const overlay = document.getElementById('setup-checklist-modal');
  if (overlay && overlay.classList.contains('open')) {
    closeSetupChecklistModal();
  }
  const target = document.querySelector(`.nav-item[data-page="${deepLink}"]`);
  if (target) {
    target.click();
  } else {
    // No matching nav-item — surface a helpful message rather than silently
    // doing nothing. Should never happen in production but adding a
    // deep-link to a non-existent page during dev would land here.
    const statusEl = _setupChecklistStatusEl(botId);
    if (statusEl) {
      statusEl.textContent = `(no nav target for "${deepLink}" — please report this)`;
      statusEl.style.color = 'var(--red)';
    }
  }
}

async function _setupChecklistSetState(botId, itemId, state) {
  const statusEl = _setupChecklistStatusEl(botId);
  if (statusEl) {
    statusEl.textContent = 'Saving…';
    statusEl.style.color = 'var(--text2)';
  }
  try {
    const data = await api(
      'POST',
      `/api/admin/bots/${encodeURIComponent(botId)}/setup-checklist/items/${encodeURIComponent(itemId)}`,
      { state },
    );
    if (!data || data.error) {
      throw new Error(data && data.error || 'unknown');
    }
    // Re-render whichever surface is mounted. Both the Settings card
    // and the Overview modal share the same renderer.
    const settingsEl = document.getElementById('botcfg-setup-checklist');
    if (settingsEl && document.getElementById(`setup-checklist-status-${botId}`) &&
        settingsEl.contains(document.getElementById(`setup-checklist-status-${botId}`))) {
      _setupChecklistRender(settingsEl, botId, data);
    }
    const modalBody = document.getElementById('setup-checklist-modal-body');
    const modalOverlay = document.getElementById('setup-checklist-modal');
    if (modalBody && modalOverlay && modalOverlay.classList.contains('open')) {
      _setupChecklistRender(modalBody, botId, data);
    }
  } catch (e) {
    if (statusEl) {
      statusEl.textContent = `Failed: ${String(e && e.message || e)}`;
      statusEl.style.color = 'var(--red)';
    }
  }
}

// ── Setup-checklist modal (Overview tile chip + Actions ⋯ menu fallback) ──
// Two entry points (same handler):
//   * Setup X/N chip on the bot tile (when is_chip_visible)
//   * "📋 Setup checklist" Actions ⋯ menu item (when should_show_in_actions_menu,
//     i.e. operator suppressed the chip but still has pending items)
// Body is rendered by _setupChecklistRender — exact same function used by
// the per-bot Settings page card. Same toggle / dismiss / suppress controls
// work inside the modal because they look up botcfg-setup-checklist-status
// for inline messaging; the modal's body uses a different container id so
// status messages only render where the operator is looking.
async function openSetupChecklistModal(botId) {
  const overlay = document.getElementById('setup-checklist-modal');
  const title = document.getElementById('setup-checklist-modal-title');
  const sub = document.getElementById('setup-checklist-modal-sub');
  const body = document.getElementById('setup-checklist-modal-body');
  if (!overlay || !body) return;
  const label = (typeof botLabel === 'function') ? botLabel(botId) : botId;
  if (title) title.textContent = `Setup checklist — ${label}`;
  if (sub) sub.textContent = 'Recommended next steps after this bot is provisioned. The same list lives at Settings → Bots for permanent access.';
  body.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  overlay.classList.add('open');
  let data;
  try {
    data = await api('GET', `/api/admin/bots/${encodeURIComponent(botId)}/setup-checklist`);
  } catch (e) {
    body.innerHTML = `<div class="subtle" style="color:var(--red)">Failed: ${escHtml(String(e && e.message || e))}</div>`;
    return;
  }
  if (!data || data.error) {
    body.innerHTML = `<div class="subtle" style="color:var(--red)">Failed to load: ${escHtml(data && data.error || 'unknown')}</div>`;
    return;
  }
  _setupChecklistRender(body, botId, data);
}

function closeSetupChecklistModal() {
  const overlay = document.getElementById('setup-checklist-modal');
  if (overlay) overlay.classList.remove('open');
  // Trigger a soft refresh of any tiles + the Settings card so chip
  // state / counter / suppression all reflect what the operator did
  // inside the modal. Best-effort — silently skip if these aren't
  // mounted on the current page.
  try {
    if (typeof refreshTiles === 'function') refreshTiles();
    else if (typeof _refreshOverview === 'function') _refreshOverview();
  } catch (_) { /* not on Overview right now */ }
  try {
    const settingsEl = document.getElementById('botcfg-setup-checklist');
    if (settingsEl && typeof _configBot !== 'undefined' && _configBot) {
      renderBotSetupChecklistCard(_configBot);
    }
  } catch (_) { /* Settings page isn't mounted */ }
}

async function _setupChecklistTileToggle(botId, action) {
  // action is "suppress" or "reset" — both endpoints return the same
  // payload shape so the renderer can swap state in place.
  const statusEl = _setupChecklistStatusEl(botId);
  if (statusEl) {
    statusEl.textContent = 'Saving…';
    statusEl.style.color = 'var(--text2)';
  }
  try {
    const data = await api(
      'POST',
      `/api/admin/bots/${encodeURIComponent(botId)}/setup-checklist/${action}`,
    );
    if (!data || data.error) {
      throw new Error(data && data.error || 'unknown');
    }
    const settingsEl = document.getElementById('botcfg-setup-checklist');
    if (settingsEl && document.getElementById(`setup-checklist-status-${botId}`) &&
        settingsEl.contains(document.getElementById(`setup-checklist-status-${botId}`))) {
      _setupChecklistRender(settingsEl, botId, data);
    }
    const modalBody = document.getElementById('setup-checklist-modal-body');
    const modalOverlay = document.getElementById('setup-checklist-modal');
    if (modalBody && modalOverlay && modalOverlay.classList.contains('open')) {
      _setupChecklistRender(modalBody, botId, data);
    }
  } catch (e) {
    if (statusEl) {
      statusEl.textContent = `Failed: ${String(e && e.message || e)}`;
      statusEl.style.color = 'var(--red)';
    }
  }
}


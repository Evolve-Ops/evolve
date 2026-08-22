// ════════════════════════════════════════════════════════════════════════
// Page: Users (with Identity sub-surface)
//
// Consolidated per-bot user-management page introduced post-V2.2-3.
// The old standalone "Identity" page (admin claim, passphrases) was
// merged into the same surface; pageRedirectRegistry maps "identity"
// → "users" so old deeplinks still resolve.
//
// Two functional clusters live here:
//
//   1. Identity machinery (function _id*, originally Phase 5 of the
//      alerts-hardening work):
//        - loadIdentity()              — pod admin + per-bot identity load
//        - _idRenderPodAdmins / _idRenderBots / _idRenderBotCard
//        - alias edit + save (_idAlias*)
//        - admin + primary claim flow (_idAdminClaim, _idPrimaryClaim,
//          _idPrimaryClaimAttempt)
//        - revoke / clear / discover-and-pick + the passphrase editor
//      Pod-wide content (admins, passphrases) renders into element
//      targets that live inside page-users via the new layout, and
//      the response is cached on window.__usersIdentityData so the
//      Users-page renderer can pick it up without re-fetching.
//
//   2. Users machinery (function loadUsers, _users*):
//        - loadUsers()                 — entry; calls loadIdentity()
//                                        and then drives bot rail +
//                                        per-bot panel
//        - the seen / approved / pending lane renderers
//        - approve / approve-seen / reject / revoke / block / unblock
//        - per-user role patch + newcomer-mode setter
//        - multi-user toggle (calls back into loadIdentity to refresh
//          the per-bot panel after the switch lands)
//
// State (function-scope inside this module):
//   _usersActiveBot, _usersPendingCounts, _usersShowEmails,
//   _usersChannelDataByBot  (Users-tier)
//   window.__usersIdentityData  (cross-tier cache — Identity → Users)
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - loadStatus() (Overview), _alLoadLane / _alLoadCount (Alerts page),
//     api(), toast(), escHtml(), botLabel() — all called by name,
//     resolved at call time from whichever script defined them.
//
// Out of scope (still inline, separate clusters):
//   _alCandidateRow / _alLoadTracked / _alSnooze / _alResolve /
//   _alPairedAct / _alDismiss* / _alRefreshActive — these are alerts
//   functions that live between the Users page block and the next
//   section header (line ~47466 pre-Phase-4a). They're part of a
//   secondary alerts cluster that wasn't captured in Phase 3f's
//   contiguous extraction and will move with the next alerts-cleanup
//   phase.
// ════════════════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════════════════════
//  Identity page — Phase 5 of alerts hardening
// ═══════════════════════════════════════════════════════════════════════════
//
// Loads the pod admin state + per-bot multi-user identity state from
// /api/admin/identity, then renders the admin claim form (pod-wide) and
// one card per multi-user bot for the primary user claim. Submits via
// POST /api/admin/identity/claim-admin and POST .../claim-primary.

async function loadIdentity() {
  const ctxEl     = document.getElementById('identity-pod-context-state');
  const podStateEl = document.getElementById('identity-pod-admin-state');
  // botsListEl is null after the Users-page split (per-bot rendering moved to
  // the bot-tile rail on page-users); _idRenderBots is now a no-op when its
  // container is missing. See docs/spec-per-bot-users-management-2026-05-29.md.
  const botsListEl = document.getElementById('identity-bots-list');
  const passEl    = document.getElementById('identity-passphrases-state');
  const nudgeEl   = document.getElementById('users-setup-nudge') || document.getElementById('identity-setup-nudge');
  if (!podStateEl) return;
  try {
    const data = await api('GET', '/api/admin/identity');
    if (ctxEl) _idRenderPodContext(ctxEl, data.pod_context || {});
    _idRenderPodAdmins(podStateEl, data.pod_admins || {});
    if (passEl) _idRenderPassphrases(passEl, data.pod_passphrases || {});
    const namesCache = (data.pod_admins || {}).resolved_names || {};
    if (botsListEl) _idRenderBots(botsListEl, data.bots || {}, namesCache);
    // Cache the latest data for the Users-page bot rail / per-bot panel
    // renderers to consume without re-fetching.
    window.__usersIdentityData = data;
    if (typeof _usersRenderAfterIdentity === 'function') {
      try { _usersRenderAfterIdentity(data); } catch (_) {}
    }
    if (nudgeEl) _idRenderSetupNudge(nudgeEl, data);
    // Best-effort lazy name resolution after the initial render —
    // batched so the user sees the cards immediately and names fill
    // in as they're discovered.
    _idLazyResolveNames(data);
  } catch (e) {
    podStateEl.innerHTML = `<div class="empty">Error: ${escHtml(String(e))}</div>`;
    botsListEl.innerHTML = '';
    if (passEl) passEl.innerHTML = '';
    if (ctxEl) ctxEl.innerHTML = '';
    if (nudgeEl) nudgeEl.style.display = 'none';
  }
}

// Render the read-only Pod Context card (Unix admin, hostname, URL).
// This is the "where am I?" panel — separating the macOS-side context
// from the messaging-side admins below.
//
// Each value gets a ``help-btn`` / ``tip`` tooltip matching the rest
// of the Identity subtab. (The earlier pattern used bare ``(?)`` text
// + browser-native ``title``; switched 2026-05-20 for visual
// consistency with the Pod Admins / Per-bot Owners cards.)
function _idRenderPodContext(el, ctx) {
  const hostname = ctx.hostname || '(unknown)';
  const url = ctx.admin_base_url || '';
  const _adminUserHtml = ctx.admin_user
    ? `<code style="background:var(--bg2);padding:2px 8px;border-radius:3px">${escHtml(ctx.admin_user)}</code>`
    : '<code style="background:var(--bg2);padding:2px 8px;border-radius:3px;color:var(--text3)"><em>not set</em></code>';
  const tipHost = (
    'macOS machine name from <code>socket.gethostname()</code>. ' +
    'Read-only because changing it would be a no-op — you change ' +
    'the hostname in System Settings → General → About → Name (or ' +
    '<code>sudo scutil --set HostName &lt;new&gt;</code>), then restart ' +
    'the admin daemon. The Admin URL re-derives automatically.'
  );
  const tipAdmin = (
    'The macOS account that runs the admin server and holds the ' +
    'sudoers grants for bot operations. Read-only because a web ' +
    'form can\'t rename a Unix user — to change it, create the new ' +
    'account with sudo on the deploy machine, then re-run ' +
    '<code>sudo evolve-admin setup</code> to migrate the grants.'
  );
  const tipUrl = (
    'Derived from the admin daemon\'s hostname (or your tailscale / ' +
    'DNS config). Override by setting <code>network.adminBaseUrl</code> ' +
    'in <code>network.json</code> or the <code>EVOLVE_ADMIN_BASE_URL</code> ' +
    'env var on the admin daemon. Restart the daemon after either ' +
    'change.'
  );
  el.innerHTML = `
    <div style="display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;align-items:baseline">
      <div style="color:var(--text2)">Deploy machine:</div>
      <div>
        <code style="background:var(--bg2);padding:2px 8px;border-radius:3px">${escHtml(hostname)}</code>
        <span class="help-btn">?<span class="tip">${tipHost}</span></span>
      </div>
      <div style="color:var(--text2)">Unix admin:</div>
      <div>
        ${_adminUserHtml}
        <span class="help-btn">?<span class="tip">${tipAdmin}</span></span>
      </div>
      <div style="color:var(--text2)">Admin URL:</div>
      <div>
        <code style="background:var(--bg2);padding:2px 8px;border-radius:3px;font-size:0.78rem">${escHtml(url)}</code>
        <span class="help-btn">?<span class="tip">${tipUrl}</span></span>
      </div>
    </div>
  `;
}

// After the initial render, walk every messaging identity we know
// about and ask the backend to resolve a human-readable name for it.
// Only fires for supported channels (telegram, slack). The backend
// caches results, so subsequent renders return instantly; this loop
// only does work on first see.
async function _idLazyResolveNames(data) {
  const seen = new Set();
  const todo = [];
  // Pod admins
  const ext = (data.pod_admins || {}).external_ids || {};
  for (const [channel, ids] of Object.entries(ext)) {
    if (!Array.isArray(ids)) continue;
    for (const id of ids) {
      const key = channel + ':' + id;
      if (!seen.has(key)) { seen.add(key); todo.push([channel, id]); }
    }
  }
  // Per-bot primary users
  for (const [, info] of Object.entries(data.bots || {})) {
    const pe = _idExtIds(info?.primary_user);
    for (const [channel, ids] of Object.entries(pe)) {
      for (const id of ids) {
        const key = channel + ':' + id;
        if (!seen.has(key)) { seen.add(key); todo.push([channel, id]); }
      }
    }
  }
  if (todo.length === 0) return;
  // Skip already-cached entries — the resolver returns instantly
  // from cache, but we'd still trigger N re-renders. Cheaper to
  // filter client-side.
  const cache = (data.pod_admins || {}).resolved_names || {};
  let didFetch = false;
  for (const [channel, id] of todo) {
    if (channel !== 'telegram' && channel !== 'slack') continue;
    const ck = channel + ':' + id;
    if (cache[ck]) continue;
    try {
      const r = await fetch('/api/admin/identity/resolve-name', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel, external_id: String(id) }),
      });
      const j = await r.json().catch(() => ({}));
      if (j && j.ok && j.resolved) {
        didFetch = true;
      }
    } catch (_) {
      // Name resolution is best-effort; failures don't block.
    }
  }
  // If we discovered any new names, re-render so they appear.
  if (didFetch) {
    try {
      const fresh = await api('GET', '/api/admin/identity');
      const freshCache = (fresh.pod_admins || {}).resolved_names || {};
      const podEl = document.getElementById('identity-pod-admin-state');
      if (podEl) _idRenderPodAdmins(podEl, fresh.pod_admins || {});
      const botsEl = document.getElementById('identity-bots-list');
      if (botsEl) _idRenderBots(botsEl, fresh.bots || {}, freshCache);
      window.__usersIdentityData = fresh;
      if (typeof _usersRenderAfterIdentity === 'function') {
        try { _usersRenderAfterIdentity(fresh); } catch (_) {}
      }
    } catch (_) {}
  }
}

// Format an external_id chip with the resolved name when available.
// Returns inner HTML for a chip. ``cache`` is the resolved_names
// dict from the API response.
function _idIdChip(channel, externalId, cache, opts) {
  opts = opts || {};
  const ck = channel + ':' + externalId;
  const resolved = cache && cache[ck];
  const id = escHtml(String(externalId));
  let label = '';
  let title = '';
  if (resolved && resolved.name) {
    const handle = resolved.username && resolved.username !== resolved.name
      ? ' @' + escHtml(resolved.username)
      : '';
    label = `<span style="color:var(--text);font-weight:500">${escHtml(resolved.name)}</span>${handle}<span style="color:var(--text3);margin-left:6px;font-size:0.75rem">${id}</span>`;
    title = `${channel}: ${externalId}` + (resolved.username ? ` (@${resolved.username})` : '');
  } else if (resolved && resolved.username) {
    label = `<span style="color:var(--text);font-weight:500">@${escHtml(resolved.username)}</span><span style="color:var(--text3);margin-left:6px;font-size:0.75rem">${id}</span>`;
    title = `${channel}: ${externalId} (@${resolved.username})`;
  } else {
    label = id;
    title = `${channel}: ${externalId} — name not resolved (try refresh, or platform may not be supported yet)`;
  }
  return { label, title };
}

// Render the "you should set this up" banner. Shown when no pod
// admin is recorded OR any multi-user bot is missing a primary.
// Drives the operator toward the right card without spelling out
// the same instructions twice.
// external_ids is `{channel: [id, ...]}` (invariant 6, M1-B2). Legacy
// per-bot blocks may still carry a bare string for a channel, so every
// read of the field goes through this — the JS twin of
// `evolve_admin.external_ids.read_external_ids`.
function _idExtIds(block) {
  const raw = (block || {}).external_ids || {};
  const out = {};
  for (const [ch, v] of Object.entries(raw)) {
    const ids = (Array.isArray(v) ? v : [v])
      .filter(x => x !== null && x !== undefined && String(x).trim() !== '')
      .map(x => String(x).trim());
    if (ids.length) out[ch] = ids;
  }
  return out;
}

function _idRenderSetupNudge(el, data) {
  const hasAdmin = Object.keys(_idExtIds(data.pod_admins)).length > 0;
  const bots = data.bots || {};
  const missingPrimaryBots = Object.entries(bots)
    .filter(([, info]) =>
      info.multi_user && Object.keys(_idExtIds(info.primary_user)).length === 0)
    .map(([id]) => id);
  const parts = [];
  if (!hasAdmin) {
    parts.push('No <strong>pod admin</strong> is recorded yet. Add one above so admin status applies pod-wide.');
  }
  if (missingPrimaryBots.length) {
    const names = missingPrimaryBots.map(id => `<code>${escHtml(id)}</code>`).join(', ');
    parts.push(`Multi-user bot${missingPrimaryBots.length > 1 ? 's' : ''} ${names} ${missingPrimaryBots.length > 1 ? 'have' : 'has'} no primary user. Set one below or share the primary passphrase with the owner so they can self-claim.`);
  }
  if (!parts.length) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  el.style.display = 'block';
  el.innerHTML = parts.map(p => `<div style="margin:2px 0">⚠ ${p}</div>`).join('');
}

function _idRenderPodAdmins(el, admins) {
  const channels = Object.entries(_idExtIds(admins));
  const names = admins.names || {};
  const cache = admins.resolved_names || {};
  if (!channels.length) {
    el.innerHTML = '<div style="color:#eb4">⚠ No pod admin recorded for any channel — every user is treated as primary by identity fallback.</div>';
    return;
  }
  // One row per (channel, external_id) pair. Each row shows the
  // resolved name (when available) + the raw ID + explicit Refresh
  // and Remove buttons. No subtle ✕ — the buttons are styled the
  // same as other admin-UI buttons so they're findable.
  const rows = channels.map(([ch, ids]) => {
    const items = ids.map(id => {
      const chip = _idIdChip(ch, id, cache);
      const idEsc = escHtml(String(id).replace(/'/g, "\\'"));
      const chEsc = escHtml(ch);
      const refresh = `<button class="btn btn-sm" onclick="_idRefreshName('${chEsc}','${idEsc}')" title="Re-fetch the name from ${chEsc}" style="padding:1px 6px;font-size:0.7rem;margin-left:6px">↻</button>`;
      const remove = `<button class="btn btn-sm btn-warning" onclick="_idRevokeAdmin('${chEsc}','${idEsc}', false)" title="Remove this channel ID from admin" style="padding:1px 8px;margin-left:4px">Remove</button>`;
      return `<div title="${chip.title}" style="display:inline-flex;align-items:center;gap:0;font-size:0.78rem;background:var(--bg2);padding:4px 8px;border-radius:3px;margin:0 6px 4px 0">${chip.label}${refresh}${remove}</div>`;
    }).join('');
    return `<div style="margin-bottom:6px"><strong style="color:var(--text2);margin-right:6px">${escHtml(ch)}:</strong>${items}</div>`;
  }).join('');
  const podUsersHtml = Array.isArray(admins.pod_users) && admins.pod_users.length
    ? `<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border);font-size:0.78rem;color:var(--text2)">
        <div style="margin-bottom:4px">People:</div>
        ${admins.pod_users.map(u => {
          const nm = names[u]
            ? `<span style="color:var(--text);font-weight:500">${escHtml(names[u])}</span> `
            : '';
          const drop = `<button class="btn btn-sm btn-warning" onclick="_idDropAdminPerson('${escHtml(String(u).replace(/'/g, "\\'"))}'); return false" title="Drop this person (clears name + label; channel IDs above remain)" style="padding:1px 8px;margin-left:6px">Drop</button>`;
          return `<span style="background:var(--bg2);padding:4px 8px;border-radius:3px;margin-right:6px;display:inline-flex;align-items:center">${nm}<code>${escHtml(u)}</code>${drop}</span>`;
        }).join(' ')}
      </div>`
    : '';
  el.innerHTML = `<div style="color:var(--text2)">${rows}</div>${podUsersHtml}`;
}

// Force-refresh a single (channel, external_id)'s resolved name.
// Calls the resolver with use_cache=false so the platform API gets
// hit even if we have a cached entry.
async function _idRefreshName(channel, externalId) {
  try {
    const r = await fetch('/api/admin/identity/resolve-name', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        channel, external_id: externalId, refresh: true,
      }),
    });
    const j = await r.json().catch(() => ({}));
    if (!j || !j.ok) {
      toast(`Name lookup failed: ${j.reason || 'unknown error'}`, 'err');
      return;
    }
    loadIdentity();
  } catch (e) {
    toast(`Request failed: ${e}`, 'err');
  }
}

// Render the Pod Passphrases card. Each passphrase has an inline
// Edit button that swaps to an input + Save/Cancel pair. Editing is
// per-passphrase so a partial save is possible.
function _idRenderPassphrases(el, pass) {
  const adminVal = pass.admin || '<em>unset</em>';
  const primaryVal = pass.primary || '<em>unset</em>';
  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
      <div style="min-width:140px;color:var(--text2)">Admin passphrase:</div>
      <code id="id-pass-admin-val" style="background:var(--bg2);padding:2px 8px;border-radius:3px">${pass.admin ? escHtml(pass.admin) : '<em>unset</em>'}</code>
      <button class="btn btn-sm" onclick="_idEditPassphrase('admin')" style="padding:3px 10px">Edit</button>
      <span id="id-pass-admin-status" class="subtle" style="font-size:0.78rem"></span>
    </div>
    <div style="display:flex;align-items:center;gap:12px">
      <div style="min-width:140px;color:var(--text2)">Primary passphrase:</div>
      <code id="id-pass-primary-val" style="background:var(--bg2);padding:2px 8px;border-radius:3px">${pass.primary ? escHtml(pass.primary) : '<em>unset</em>'}</code>
      <button class="btn btn-sm" onclick="_idEditPassphrase('primary')" style="padding:3px 10px">Edit</button>
      <span id="id-pass-primary-status" class="subtle" style="font-size:0.78rem"></span>
    </div>
    <div class="subtle" style="margin-top:8px;font-size:0.74rem">
      Defaults: <code>charles</code> (admin), <code>darwin</code> (primary). Bots can override the primary passphrase below; the admin passphrase is always pod-wide.
    </div>
  `;
}

function _idEditPassphrase(kind) {
  const valEl = document.getElementById(`id-pass-${kind}-val`);
  if (!valEl || valEl.dataset.editing === '1') return;
  valEl.dataset.editing = '1';
  const current = valEl.textContent.startsWith('<') ? '' : valEl.textContent;
  valEl.innerHTML = `<input id="id-pass-${kind}-input" type="text" value="${escHtml(current)}" style="padding:2px 6px;background:var(--bg2);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:0.82rem;width:180px"> <button class="btn btn-primary btn-sm" onclick="_idSavePassphrase('${kind}')" style="padding:2px 8px">Save</button> <button class="btn btn-sm" onclick="loadIdentity()" style="padding:2px 8px">Cancel</button>`;
  document.getElementById(`id-pass-${kind}-input`)?.focus();
}

async function _idSavePassphrase(kind) {
  const input = document.getElementById(`id-pass-${kind}-input`);
  const status = document.getElementById(`id-pass-${kind}-status`);
  if (!input || !status) return;
  const passphrase = input.value.trim();
  status.style.color = 'var(--muted)';
  status.textContent = 'Saving…';
  try {
    const r = await fetch('/api/admin/identity/set-pod-passphrase', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind, passphrase: passphrase || null }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      status.style.color = '#ff8c42';
      status.textContent = `Error: ${j.error || ('HTTP ' + r.status)}`;
      return;
    }
    status.style.color = '#7fff9e';
    status.textContent = '✓ saved';
    loadIdentity();
  } catch (e) {
    status.style.color = '#ff8c42';
    status.textContent = `Request failed: ${e}`;
  }
}

async function _idRevokeAdmin(channel, externalId, dropPodUser) {
  if (!await confirmModal({body: `Remove admin ${externalId} on ${channel}?`, danger: true})) return;
  try {
    const r = await fetch('/api/admin/identity/revoke-admin', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        channel, external_id: externalId,
        drop_pod_user: dropPodUser || undefined,
      }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      toast(`Error: ${j.error || ('HTTP ' + r.status)}`, 'err');
      return;
    }
    loadIdentity();
  } catch (e) {
    toast(`Request failed: ${e}`, 'err');
  }
}

async function _idDropAdminPerson(podUser) {
  // Drop a pod_user — clears the name + every channel ID associated
  // with that pod_user across the admin record. This is a more
  // aggressive revoke than removing a single channel ID; we don't
  // know which channel IDs belong to the same person, so this
  // currently relies on the backend's drop_pod_user flag rather
  // than enumerating per-channel revokes.
  //
  // NOTE: drop_pod_user removes the entry from pod_users + names,
  // but the per-channel external_ids stay until you call revoke per
  // channel. For the strict "remove the whole person" semantics,
  // the operator can use the CLI; this UI flow drops the name + label
  // but preserves the IDs so a partial cleanup is reversible.
  if (!await confirmModal({body: `Drop person "${podUser}" (clears the display name; channel IDs stay)?`, danger: true})) return;
  try {
    // We send a placeholder external_id since the route requires one;
    // pass drop_pod_user to do the real work. Backend revoke is
    // idempotent on the channel-id side.
    const r = await fetch('/api/admin/identity/revoke-admin', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        channel: 'telegram', external_id: '__drop_marker__',
        drop_pod_user: podUser,
      }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      toast(`Error: ${j.error || ('HTTP ' + r.status)}`, 'err');
      return;
    }
    loadIdentity();
  } catch (e) {
    toast(`Request failed: ${e}`, 'err');
  }
}

function _idRenderBots(el, bots, cache) {
  const entries = Object.entries(bots);
  if (!entries.length) {
    el.innerHTML = '<div class="empty">No bots configured.</div>';
    return;
  }
  // Multi-user bots first, then single-user; within each group, by name.
  entries.sort(([a, ai], [b, bi]) => {
    if (ai.multi_user !== bi.multi_user) return bi.multi_user ? 1 : -1;
    return a < b ? -1 : a > b ? 1 : 0;
  });
  el.innerHTML = entries.map(([botId, info]) => _idRenderBotCard(botId, info, cache || {})).join('');
  // Kick off alias loads in parallel — each card has its own
  // id-alias-body-<botId> placeholder; the GET is cheap and async.
  for (const [botId] of entries) {
    if (typeof _idLoadAliasForBot === 'function') {
      _idLoadAliasForBot(botId);
    }
  }
}

function _idRenderBotCard(botId, info, cache) {
  cache = cache || {};
  const primary = info.primary_user || {};
  const externalIds = _idExtIds(primary);
  const hasAny = Object.keys(externalIds).length > 0;
  const multiUser = info.multi_user;
  // Single-user bots get the same form — labelled "Owner" — so the
  // operator can capture a soft owner identity for alert routing
  // fallback and identifying non-owner messages. OC doesn't partition
  // sessions for single-user bots, so this is metadata, not enforcement.
  const heading = multiUser ? 'Primary user' : 'Owner';

  // H.1 — Compact "Current primary user" display. Replaces the busy
  // mix of chips/name/pod_user/Clear that previously sprawled across
  // the top row. Operator's question on this surface is just "who is
  // the primary, can I see at a glance" — answer with one tidy line.
  let currentDisplay;
  if (hasAny) {
    // One chip per (channel, id) pair — a person may hold several ids
    // on one channel (invariant 6), and all of them are real.
    const chipRows = Object.entries(externalIds)
      .flatMap(([ch, ids]) => ids.map(id => {
        const chip = _idIdChip(ch, id, cache);
        return `<span title="${chip.title}" style="background:var(--bg2);padding:3px 8px;border-radius:3px;font-size:0.78rem"><span style="color:var(--text3);margin-right:4px">${escHtml(ch)}:</span>${chip.label}</span>`;
      }))
      .join(' ');
    const nameBit = primary.name
      ? `<strong style="color:var(--text)">${escHtml(primary.name)}</strong>`
      : '';
    const pidBit = primary.pod_user
      ? `<span style="font-size:0.74rem;color:var(--text2)">Person ID: <code style="background:var(--bg2);padding:2px 6px;border-radius:3px" title="Wire name: pod_user">${escHtml(primary.pod_user)}</code></span>`
      : '';
    currentDisplay = `${nameBit ? nameBit + ' &nbsp; ' : ''}${chipRows}${pidBit ? ' &nbsp; ' + pidBit : ''}`;
  } else {
    currentDisplay = `<span style="color:#eb4">⚠ no ${heading.toLowerCase()} recorded</span>`;
  }

  // H.1 — Single Change/Set button replaces the "always-visible form"
  // pattern. Form is hidden by default when a primary is set so the
  // operator's eye lands on the current value, not the editing UI.
  // No primary → form starts expanded so the action is one click away.
  const actionLabel = hasAny ? `Change ${heading.toLowerCase()}` : `Set ${heading.toLowerCase()}`;
  const actionBtn = `<button class="btn btn-sm btn-primary" onclick="_idToggleEditPrimary('${escHtml(botId)}')" style="padding:3px 10px;font-size:0.76rem">${escHtml(actionLabel)}</button>`;
  const clearBtn = hasAny
    ? `<button class="btn btn-sm btn-warning" onclick="_idClearPrimary('${escHtml(botId)}')" title="Remove ALL channel IDs for this bot's ${heading.toLowerCase()}" style="padding:3px 10px;font-size:0.72rem">Clear</button>`
    : '';

  // H.4 — Bot name + multi-user badge moved up to the Users panel
  // header (bot-name uppercase + Single-user|Multi-user toggle is
  // already there); repeating them here below the "Owner / primary
  // user" section header was redundant. Card now starts directly
  // with the "Current primary user" row.
  const formHidden = hasAny ? 'display:none;' : '';
  return `
    <div style="padding:12px 0;border-bottom:1px solid var(--border)">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px">
        <span style="font-size:0.74rem;color:var(--text2);min-width:130px">Current ${heading.toLowerCase()}:</span>
        <span style="flex:1;display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap">${currentDisplay}</span>
        ${actionBtn}
        ${clearBtn}
      </div>
      <div id="id-pri-form-${escHtml(botId)}" style="${formHidden}padding:10px 12px;background:var(--bg2);border-radius:6px;margin-top:6px">
        <div style="font-size:0.78rem;color:var(--text2);margin-bottom:6px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <span>${hasAny ? 'Change' : 'Set'} ${heading.toLowerCase()} — pick channel + external_id; if a primary is already set for this channel, you'll be asked to confirm the replacement.</span>
          <button class="btn btn-sm" onclick="_idDiscoverPrimary('${escHtml(botId)}')" title="Scan recent turn history to suggest likely ${heading.toLowerCase()} candidates from this bot's actual users" style="padding:2px 8px;font-size:0.72rem">Discover from history</button>
        </div>
        <div id="id-pri-discover-${escHtml(botId)}" style="font-size:0.78rem;margin-bottom:8px"></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:end;margin-bottom:8px">
          <div>
            <label style="display:block;font-size:0.72rem;color:var(--text2);margin-bottom:3px">Channel</label>
            <select id="id-pri-ch-${escHtml(botId)}" class="form-select input-w-sm" style="padding:5px 7px;font-size:0.82rem">
              <option value="telegram">Telegram</option>
              <option value="slack">Slack</option>
              <option value="discord">Discord</option>
              <option value="whatsapp">WhatsApp</option>
            </select>
          </div>
          <div style="flex:1;min-width:180px">
            <label style="display:block;font-size:0.72rem;color:var(--text2);margin-bottom:3px">External ID</label>
            <input id="id-pri-eid-${escHtml(botId)}" type="text" placeholder="e.g. 123456789" style="width:100%;padding:5px 7px;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:0.82rem">
          </div>
          <div style="flex:1;min-width:120px">
            <label style="display:block;font-size:0.72rem;color:var(--text2);margin-bottom:3px" title="Stable label tying multiple channel IDs to one person. Wire name: pod_user.">Person ID (optional)</label>
            <input id="id-pri-pu-${escHtml(botId)}" type="text" placeholder="pod_admin_user" style="width:100%;padding:5px 7px;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:0.82rem">
          </div>
          <div style="flex:1;min-width:120px">
            <label style="display:block;font-size:0.72rem;color:var(--text2);margin-bottom:3px">Display name (optional)</label>
            <input id="id-pri-name-${escHtml(botId)}" type="text" placeholder="Marcus" style="width:100%;padding:5px 7px;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:0.82rem">
          </div>
          <button class="btn btn-primary btn-sm" onclick="_idPrimaryClaim('${escHtml(botId)}')" title="If a primary is already set for this channel, you'll be asked to confirm the replacement.">Set ${escHtml(heading.toLowerCase())}</button>
        </div>
        <div id="id-pri-status-${escHtml(botId)}" class="subtle" style="margin-top:6px;font-size:0.74rem;min-height:1.05em"></div>
      </div>
    </div>`;
}

// H.1 — Show / hide the primary-user form. Called from the Change /
// Set primary user button. Toggles a placeholder div whose initial
// state depends on whether a primary is already set.
function _idToggleEditPrimary(botId) {
  const form = document.getElementById(`id-pri-form-${botId}`);
  if (!form) return;
  const showing = form.style.display !== 'none';
  form.style.display = showing ? 'none' : '';
}

// H.3 — Email-alias section, extracted from _idRenderBotCard. The
// placeholder (#id-alias-body-${botId}) used to live inside the
// primary-user card; that conflated identity (who is the owner?)
// with outbound mail config (how does outbound mail look?). After
// H.3 the alias renders in its own tile below the channel/users
// blocks. _idLoadAliasForBot still drives it.
function _idRenderAliasSection(botId) {
  return `
    <div id="id-alias-${escHtml(botId)}" style="margin-top:14px;padding:12px 14px;border:1px solid var(--border);border-radius:6px;background:var(--bg2)">
      <div style="font-size:0.85rem;font-weight:600;margin-bottom:4px">Email alias</div>
      <div style="font-size:0.74rem;color:var(--text2);margin-bottom:8px">The name + email used in From headers when this bot sends mail.</div>
      <div id="id-alias-body-${escHtml(botId)}" style="font-size:0.78rem;color:var(--text2)"><em>Loading…</em></div>
    </div>`;
}

// ── Per-bot alias editor (writes only the correspondence block) ────────────
//
// Backed by GET/PUT /api/bot/<id>/alias. Lives inline inside each
// bot's identity card on the Identity tab — the natural place an
// operator goes after setting the primary user ("now make the bot
// send mail as that user's name"). Distinct from the path-C Google
// wizard (which also writes the alias, alongside SA + scopes +
// subject) — this surface exists so renaming the alias is a one-form
// edit, not a six-screen wizard re-run.

async function _idLoadAliasForBot(botId) {
  const body = document.getElementById(`id-alias-body-${botId}`);
  if (!body) return;
  try {
    const r = await fetch(`/api/bot/${encodeURIComponent(botId)}/alias`);
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      body.innerHTML = `<span style="color:var(--orange)">Could not load alias: ${escHtml((j && j.error) || ('HTTP ' + r.status))}</span>`;
      return;
    }
    _idRenderAliasCard(botId, j);
  } catch (e) {
    body.innerHTML = `<span style="color:var(--orange)">Request failed: ${escHtml(String(e))}</span>`;
  }
}

function _idRenderAliasCard(botId, info) {
  const body = document.getElementById(`id-alias-body-${botId}`);
  if (!body) return;
  const alias = info.alias || {};
  const suggestedName = info.suggested_name || '';
  const hasName = !!alias.name;
  const currentName = alias.name || suggestedName || '';
  const currentEmail = alias.email_address || '';
  const currentDisclosure = alias.disclosure || 'soft';
  const overrideReason = alias.disclosure_override_reason || '';
  const mailbox = info.workspace_mailbox || '';
  const multiUser = !!info.multi_user;
  const hasMailbox = !!info.has_workspace_mailbox;

  // Status chip: configured / suggested / unconfigured
  let statusChip;
  if (hasName) {
    statusChip = `<span class="badge badge-muted" style="font-size:0.7rem;background:rgba(52,168,83,0.15);color:#7fff9e">configured</span>`;
  } else if (suggestedName) {
    statusChip = `<span class="badge badge-muted" style="font-size:0.7rem">unset — suggested: ${escHtml(suggestedName)}</span>`;
  } else {
    statusChip = `<span class="badge badge-muted" style="font-size:0.7rem">unset — internal-only</span>`;
  }

  // Multi-user hint links to the spec
  const multiUserHint = multiUser
    ? `<div style="background:var(--bg2);padding:6px 8px;border-radius:4px;margin-bottom:8px;font-size:0.72rem;color:var(--text2);border-left:3px solid var(--accent)">
         This is a multi-user bot — the alias below applies to every outbound mail today. Per-user rotation (sending as the initiating user's name) is specced at <code>docs/spec-multi-user-alias-2026-06-01.md</code> and tracked as deliverable C.
       </div>`
    : '';

  // No mailbox = nothing to send from. Surface that clearly so the
  // operator knows the alias is inert until path-C is configured.
  const mailboxHint = hasMailbox
    ? `<div style="font-size:0.7rem;color:var(--text3);margin-bottom:6px">Workspace mailbox: <code>${escHtml(mailbox)}</code></div>`
    : `<div style="font-size:0.7rem;color:var(--orange);margin-bottom:6px">No Workspace mailbox configured — alias is inert until you finish the Google Workspace wizard.</div>`;

  // The hint under the name field: when alias = user's name, that's the
  // natural EA pattern (Sam's bot signs mail as "Sam"). When alias is
  // different, that's a deliberate "wear a costume" choice. The hint
  // text tracks which case the operator's in.
  const nameHint = suggestedName
    ? (currentName.toLowerCase() === suggestedName.toLowerCase()
        ? `Pre-filled from primary user — this is the natural choice for EA-style bots.`
        : `Different from primary user (${escHtml(suggestedName)}). That's intentional only if you want a fictional vendor-facing identity.`)
    : `Set the name vendors will see in the From header. Leave blank to make this bot internal-only.`;

  body.innerHTML = `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">
      ${statusChip}
      ${hasName ? `<span style="color:var(--text1);font-weight:500">${escHtml(alias.name)}</span>` : ''}
      ${currentEmail ? `<code style="background:var(--bg2);padding:2px 6px;border-radius:3px;font-size:0.74rem">${escHtml(currentEmail)}</code>` : ''}
      ${hasName ? `<span style="color:var(--text3);font-size:0.72rem">disclosure: ${escHtml(currentDisclosure)}</span>` : ''}
    </div>
    ${mailboxHint}
    ${multiUserHint}
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:end;margin-bottom:6px">
      <div style="flex:1;min-width:160px">
        <label style="display:block;font-size:0.7rem;color:var(--text2);margin-bottom:3px">Alias name</label>
        <input id="id-alias-name-${escHtml(botId)}" type="text" value="${escHtml(currentName)}" placeholder="(blank = internal-only)" style="width:100%;padding:5px 7px;background:var(--bg2);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:0.82rem">
        <div style="font-size:0.68rem;color:var(--text3);margin-top:3px">${nameHint}</div>
      </div>
      <div style="flex:1;min-width:160px">
        <label style="display:block;font-size:0.7rem;color:var(--text2);margin-bottom:3px">Send-as email <span style="color:var(--text3)">(optional)</span></label>
        <input id="id-alias-email-${escHtml(botId)}" type="email" value="${escHtml(currentEmail)}" placeholder="${escHtml(mailbox || '(uses Workspace mailbox)')}" style="width:100%;padding:5px 7px;background:var(--bg2);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:0.82rem;font-family:ui-monospace,Menlo,monospace">
      </div>
      <div style="min-width:200px">
        <label style="display:block;font-size:0.7rem;color:var(--text2);margin-bottom:3px">Disclosure</label>
        <select id="id-alias-disclosure-${escHtml(botId)}" class="form-select input-w-full" onchange="_idAliasDisclosureChanged('${escHtml(botId)}')" style="padding:5px 7px;font-size:0.82rem">
          <option value="explicit"${currentDisclosure === 'explicit' ? ' selected' : ''}>Explicit — "AI assistant to {user}"</option>
          <option value="soft"${currentDisclosure === 'soft' ? ' selected' : ''}>Soft — "Assistant to {user}"</option>
          <option value="none"${currentDisclosure === 'none' ? ' selected' : ''}>None — just the alias name</option>
        </select>
      </div>
      <button class="btn btn-primary btn-sm" onclick="_idAliasSave('${escHtml(botId)}')" style="padding:4px 12px">Save alias</button>
    </div>
    <div id="id-alias-reason-row-${escHtml(botId)}" style="display:${currentDisclosure === 'none' ? '' : 'none'};margin-bottom:6px">
      <label style="display:block;font-size:0.7rem;color:var(--text2);margin-bottom:3px">Why disclosure="none"? <span style="color:var(--orange)">(required; recorded in audit log)</span></label>
      <input id="id-alias-reason-${escHtml(botId)}" type="text" value="${escHtml(overrideReason)}" placeholder="e.g. long-standing vendor relationships predate the bot" style="width:100%;padding:5px 7px;background:var(--bg2);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:0.82rem">
    </div>
    <div id="id-alias-warn-${escHtml(botId)}" style="display:none;font-size:0.7rem;color:var(--orange);margin-bottom:4px;line-height:1.4"></div>
    <div id="id-alias-status-${escHtml(botId)}" class="subtle" style="font-size:0.72rem;min-height:1em"></div>
  `;
  _idAliasUpdateWarning(botId, suggestedName);
  const nameInput = document.getElementById(`id-alias-name-${botId}`);
  if (nameInput) nameInput.oninput = () => _idAliasUpdateWarning(botId, suggestedName);
}

function _idAliasDisclosureChanged(botId) {
  const sel = document.getElementById(`id-alias-disclosure-${botId}`);
  const reasonRow = document.getElementById(`id-alias-reason-row-${botId}`);
  if (sel && reasonRow) reasonRow.style.display = (sel.value === 'none') ? '' : 'none';
  // Re-evaluate the ethical-disclosure warning whenever disclosure changes
  // — the suggestedName closure isn't available here, so re-read from
  // the page via a small DOM probe (the suggested-name chip stores it).
  const nameEl = document.getElementById(`id-alias-name-${botId}`);
  const suggestedFromChip = (document.querySelector(`#id-alias-body-${botId} .badge`) || {}).textContent || '';
  const m = suggestedFromChip.match(/suggested: (.+)$/);
  const suggested = m ? m[1].trim() : (nameEl ? nameEl.value : '');
  _idAliasUpdateWarning(botId, suggested);
}

function _idAliasUpdateWarning(botId, suggestedName) {
  // Surface the ethics caution when alias = user's name AND disclosure
  // is "none" — mail looks self-authored, which is misrepresentation
  // to the recipient. The UI never blocks; only informs.
  const warn = document.getElementById(`id-alias-warn-${botId}`);
  if (!warn) return;
  const nameEl = document.getElementById(`id-alias-name-${botId}`);
  const discEl = document.getElementById(`id-alias-disclosure-${botId}`);
  if (!nameEl || !discEl) return;
  const aliasName = nameEl.value.trim();
  if (discEl.value === 'none' && suggestedName && aliasName &&
      aliasName.toLowerCase() === suggestedName.toLowerCase()) {
    warn.textContent = (
      '⚠ Alias is the user\'s own name and disclosure is "none" — mail ' +
      'will look like the user wrote it personally. Pick "soft" or ' +
      '"explicit" unless you have a documented reason.'
    );
    warn.style.display = '';
  } else {
    warn.style.display = 'none';
  }
}

async function _idAliasSave(botId) {
  const status = document.getElementById(`id-alias-status-${botId}`);
  const nameEl = document.getElementById(`id-alias-name-${botId}`);
  const emailEl = document.getElementById(`id-alias-email-${botId}`);
  const discEl = document.getElementById(`id-alias-disclosure-${botId}`);
  const reasonEl = document.getElementById(`id-alias-reason-${botId}`);
  if (!status || !nameEl || !discEl) return;

  const name = nameEl.value.trim();
  const disclosure = discEl.value;
  const reason = (reasonEl && reasonEl.value || '').trim();

  // Client-side guard for disclosure=none so the operator gets an
  // immediate hint rather than a 400 round-trip.
  if (name && disclosure === 'none' && !reason) {
    status.style.color = 'var(--orange)';
    status.textContent = '⚠ A reason is required when disclosure is "none".';
    return;
  }

  status.style.color = 'var(--muted)';
  status.textContent = 'Saving…';
  const body = { name: name, disclosure: disclosure };
  if (emailEl && emailEl.value.trim()) body.email_address = emailEl.value.trim();
  if (disclosure === 'none' && reason) body.disclosure_override_reason = reason;

  try {
    const r = await fetch(`/api/bot/${encodeURIComponent(botId)}/alias`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      const errs = (j && j.errors) || [(j && j.error) || ('HTTP ' + r.status)];
      status.style.color = 'var(--orange)';
      status.innerHTML = errs.map(e => '⚠ ' + escHtml(e)).join('<br>');
      return;
    }
    status.style.color = '#7fff9e';
    status.textContent = j.cleared ? '✓ Alias cleared' : '✓ Saved';
    // Re-render so chips + hints update without another round-trip.
    _idRenderAliasCard(botId, j);
  } catch (e) {
    status.style.color = 'var(--orange)';
    status.textContent = `Request failed: ${e}`;
  }
}

// Clear the recorded primary/owner identity on a bot — calls
// claim-primary with force=true and an empty external_id? No,
// the API doesn't support clearing that way. Instead we use a new
// route that resets the primary_user block. For now, this is a
// best-effort delete via direct network-edit — feature flag-ish.
async function _idClearPrimary(botId) {
  if (!await confirmModal({body: `Clear the recorded owner/primary for ${botLabel(botId)}? You can re-set it after.`, danger: true})) return;
  try {
    const r = await fetch('/api/admin/identity/clear-primary', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bot_id: botId }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      toast(`Error: ${j.error || ('HTTP ' + r.status)}`, 'err');
      return;
    }
    loadIdentity();
  } catch (e) {
    toast(`Request failed: ${e}`, 'err');
  }
}

// Discover candidate primary/owner identities from the bot's actual
// turn history. Hits POST /api/admin/identity/discover-primary which
// scans turns-*.jsonl over the lookback window, tallies (channel,
// external_id) pairs from human messages, and runs each through the
// name resolver. Operator picks one → it auto-fills the form fields
// for confirmation (NOT immediate save — operator hits "Set primary
// user" themselves once they're happy).
async function _idDiscoverPrimary(botId) {
  const target = document.getElementById(`id-pri-discover-${botId}`);
  if (!target) return;
  target.innerHTML = '<span style="color:var(--text2)">Scanning turn history…</span>';
  try {
    const r = await fetch('/api/admin/identity/discover-primary', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bot_id: botId, lookback_days: 30, top_k: 5 }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      target.innerHTML = `<span style="color:#eb4">Discovery failed: ${escHtml(j.error || j.reason || ('HTTP ' + r.status))}</span>`;
      return;
    }
    const cands = j.candidates || [];
    if (!cands.length) {
      target.innerHTML = `<span style="color:var(--text2)">No human turn history found in the last 30 days. Either the bot is new, hasn't been used yet, or has only cron/subagent activity. Enter manually below.</span>`;
      return;
    }
    // Render the candidates as picker rows. Most-likely owner is
    // the top row (sorted by turn count desc + last_seen desc); the
    // operator can pick a different one if usage is balanced.
    const rows = cands.map((c, i) => {
      const nameBit = c.display_name
        ? `<strong>${escHtml(c.display_name)}</strong>`
        : '<span style="color:var(--text2)"><em>(no name)</em></span>';
      const userBit = c.username
        ? ` <span style="color:var(--text2)">@${escHtml(c.username)}</span>`
        : '';
      const lastSeen = c.last_seen ? c.last_seen.slice(0, 10) : '—';
      const rank = i === 0 ? '<span class="badge badge-muted" style="font-size:0.7rem;margin-right:6px">most likely</span>' : '';
      // Build the onclick args. Pass the display name as a single-
      // quoted string with HTML-entity-escaped content so the browser
      // decodes it back to a JS string at click time. Avoids the
      // double-quote collision JSON.stringify would cause inside an
      // onclick="..." attribute.
      const dnEsc = escHtml(c.display_name || '').replace(/'/g, '&#39;');
      const args = `'${escHtml(botId)}','${escHtml(c.channel)}','${escHtml(c.external_id)}','${dnEsc}'`;
      return `
        <div style="display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--border);flex-wrap:wrap">
          ${rank}
          <span style="color:var(--text3);min-width:60px">${escHtml(c.channel)}</span>
          <code style="background:var(--bg2);padding:2px 6px;border-radius:3px;font-size:0.78rem">${escHtml(c.external_id)}</code>
          ${nameBit}${userBit}
          <span style="color:var(--text2);font-size:0.74rem;margin-left:auto">${c.turn_count} turns · last ${escHtml(lastSeen)}</span>
          <button class="btn btn-sm" onclick="_idDiscoverPickAndSet(${args})" style="padding:2px 8px;font-size:0.72rem">Use</button>
        </div>`;
    }).join('');
    target.innerHTML = `
      <div style="background:var(--bg2);padding:8px 10px;border-radius:4px;border-left:3px solid var(--accent)">
        <div style="font-weight:600;margin-bottom:4px">Candidates from last 30 days of turns</div>
        <div style="color:var(--text2);font-size:0.74rem;margin-bottom:6px">Pick the right one — fills the form below; you confirm by clicking Set.</div>
        ${rows}
      </div>`;
  } catch (e) {
    target.innerHTML = `<span style="color:#eb4">Request failed: ${escHtml(String(e))}</span>`;
  }
}

// Operator picked a candidate row — fill in the form fields so they
// can review/edit before saving. Done as auto-fill (not auto-save)
// because the operator might want to add a pod_user or correct the
// display name first.
function _idDiscoverPick(botId, channel, externalId, displayName) {
  const ch = document.getElementById(`id-pri-ch-${botId}`);
  const eid = document.getElementById(`id-pri-eid-${botId}`);
  const name = document.getElementById(`id-pri-name-${botId}`);
  const status = document.getElementById(`id-pri-status-${botId}`);
  if (ch) ch.value = channel;
  if (eid) eid.value = externalId;
  if (name && displayName && !name.value) name.value = displayName;
  if (status) status.textContent = `Loaded ${channel} ${externalId} into the form — review and click Set ${name ? '' : ''} to confirm.`;
  // Smooth-scroll the form into view so the operator sees the next step.
  eid?.focus();
}

// One-click flow: fill the form from the discovered candidate AND
// immediately commit it. Operators clicking "Use" on a candidate row
// expect the action to actually set the owner — the previous two-step
// (Use → Set owner) confused operators 2026-05-29.
//
// If the existing primary already matches, the API is idempotent and
// returns 200. If a different primary is already recorded, the API
// rejects with "already recorded — pass force=true", which surfaces as
// an error in the form's status line; the operator can then check the
// "overwrite" box and retry.
async function _idDiscoverPickAndSet(botId, channel, externalId, displayName) {
  _idDiscoverPick(botId, channel, externalId, displayName);
  // Tiny tick so the form-fill DOM mutations land before claim reads them.
  await Promise.resolve();
  await _idPrimaryClaim(botId);
}

function _idEditBotPassphrase(botId) {
  const valEl = document.getElementById(`id-bot-pass-val-${botId}`);
  if (!valEl || valEl.dataset.editing === '1') return;
  valEl.dataset.editing = '1';
  // Use the displayed text if it's a real override; ignore the
  // <em>inherits</em> placeholder.
  const cur = valEl.textContent.startsWith('inherits') ? '' : valEl.textContent;
  valEl.innerHTML = `<input id="id-bot-pass-input-${escHtml(botId)}" type="text" value="${escHtml(cur)}" placeholder="(leave blank to inherit)" style="padding:2px 6px;background:var(--bg2);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:0.82rem;width:160px"> <button class="btn btn-primary btn-sm" onclick="_idSaveBotPassphrase('${escHtml(botId)}')" style="padding:2px 8px">Save</button> <button class="btn btn-sm" onclick="loadIdentity()" style="padding:2px 8px">Cancel</button>`;
  document.getElementById(`id-bot-pass-input-${botId}`)?.focus();
}

async function _idSaveBotPassphrase(botId) {
  const input = document.getElementById(`id-bot-pass-input-${botId}`);
  const status = document.getElementById(`id-bot-pass-status-${botId}`);
  if (!input || !status) return;
  const passphrase = input.value.trim();
  status.style.color = 'var(--muted)';
  status.textContent = 'Saving…';
  try {
    const r = await fetch('/api/admin/identity/set-bot-passphrase', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bot_id: botId, passphrase: passphrase || null }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      status.style.color = '#ff8c42';
      status.textContent = `Error: ${j.error || ('HTTP ' + r.status)}`;
      return;
    }
    status.style.color = '#7fff9e';
    status.textContent = passphrase ? '✓ saved' : '✓ reverted to pod default';
    loadIdentity();
  } catch (e) {
    status.style.color = '#ff8c42';
    status.textContent = `Request failed: ${e}`;
  }
}

async function _idAdminClaim() {
  const ch = document.getElementById('identity-admin-channel');
  const eid = document.getElementById('identity-admin-external-id');
  const pu = document.getElementById('identity-admin-pod-user');
  const nm = document.getElementById('identity-admin-name');
  const status = document.getElementById('identity-admin-status');
  if (!ch || !eid || !status) return;
  const channel = ch.value;
  const externalId = (eid.value || '').trim();
  const podUser = (pu && pu.value || '').trim();
  const name = (nm && nm.value || '').trim();
  if (!externalId) {
    status.style.color = '#ff8c42';
    status.textContent = 'External ID is required.';
    return;
  }
  if (name && !podUser) {
    status.style.color = '#ff8c42';
    status.textContent = 'Display name needs a pod user to attach to. Add one or leave Name blank.';
    return;
  }
  status.style.color = 'var(--muted)';
  status.textContent = 'Submitting…';
  try {
    const r = await fetch('/api/admin/identity/claim-admin', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        channel,
        external_id: externalId,
        pod_user: podUser || undefined,
        name: name || undefined,
      }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      status.style.color = '#ff8c42';
      status.textContent = `Error: ${j.error || ('HTTP ' + r.status)}`;
      return;
    }
    status.style.color = '#7fff9e';
    status.textContent = `✓ Added ${externalId} as admin on ${channel}.`;
    eid.value = '';
    if (pu) pu.value = '';
    if (nm) nm.value = '';
    loadIdentity();  // refresh state
  } catch (e) {
    status.style.color = '#ff8c42';
    status.textContent = `Request failed: ${e}`;
  }
}

async function _idPrimaryClaim(botId) {
  const ch = document.getElementById(`id-pri-ch-${botId}`);
  const eid = document.getElementById(`id-pri-eid-${botId}`);
  const pu = document.getElementById(`id-pri-pu-${botId}`);
  const nm = document.getElementById(`id-pri-name-${botId}`);
  const status = document.getElementById(`id-pri-status-${botId}`);
  if (!ch || !eid || !status) return;
  const channel = ch.value;
  const externalId = (eid.value || '').trim();
  const podUser = (pu && pu.value || '').trim();
  const name = (nm && nm.value || '').trim();
  if (!externalId) {
    status.style.color = '#ff8c42';
    status.textContent = 'External ID is required.';
    return;
  }
  // Phase C.5 — single-button UX. Try without force first; if the
  // server reports an existing primary, prompt the operator to confirm
  // the replacement, then retry with force=true. Replaces the prior
  // "↺ overwrite" checkbox + button two-step which made operators
  // wonder why the button didn't just do the thing.
  await _idPrimaryClaimAttempt(
    botId, channel, externalId, podUser, name, /* force */ false,
    /* statusEl */ status, /* idEl */ eid, /* puEl */ pu, /* nmEl */ nm,
  );
}

async function _idPrimaryClaimAttempt(
  botId, channel, externalId, podUser, name, force,
  status, eid, pu, nm,
) {
  status.style.color = 'var(--muted)';
  status.textContent = 'Submitting…';
  try {
    const r = await fetch('/api/admin/identity/claim-primary', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        bot_id: botId, channel, external_id: externalId,
        pod_user: podUser || undefined,
        name: name || undefined,
        force,
      }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      // Server's "already recorded" shape (per identity.claim_primary):
      //   "primary for 'X' on channel 'Y' is already recorded as 'Z';
      //    pass force=True to overwrite"
      const err = j.error || ('HTTP ' + r.status);
      if (!force && /pass force=True to overwrite/i.test(err)) {
        // Extract the existing id from the message to make the confirm
        // dialog concrete. Best-effort — fall back to a generic prompt
        // if the regex misses.
        const existingMatch = err.match(/already recorded as '([^']+)'/);
        const existingId = existingMatch ? existingMatch[1] : '(existing)';
        const ok = await confirmModal({body: (
          `${botLabel(botId)} already has a primary on ${channel}: ${existingId}\n\n` +
          `Replace with ${externalId}?`
        ), danger: true});
        if (!ok) {
          status.style.color = 'var(--text2)';
          status.textContent = 'Cancelled — existing primary unchanged.';
          return;
        }
        // Retry with force=true. Same handler — recurses once.
        return _idPrimaryClaimAttempt(
          botId, channel, externalId, podUser, name, true,
          status, eid, pu, nm,
        );
      }
      status.style.color = '#ff8c42';
      status.textContent = `Error: ${err}`;
      return;
    }
    // Setting the owner now also auto-approves that owner's DM and records
    // the chat_id (server-side seed_channel_identity). Reflect that in the
    // confirmation so the operator knows the bot can actually reach them —
    // and warn if the DM-approval / chat_id side partially failed.
    const seedWarnings = Array.isArray(j.seed_warnings) ? j.seed_warnings : [];
    if (seedWarnings.length) {
      status.style.color = '#ff8c42';
      status.textContent =
        `✓ ${botLabel(botId)}: owner set to ${externalId} on ${channel}, ` +
        `but DM auto-approve / chat ID had issues: ${seedWarnings.join('; ')}`;
    } else {
      status.style.color = '#7fff9e';
      status.textContent =
        `✓ ${botLabel(botId)}: owner set to ${externalId} on ${channel} ` +
        `(DM auto-approved).`;
    }
    eid.value = '';
    if (pu) pu.value = '';
    if (nm) nm.value = '';
    loadIdentity();  // refresh state
  } catch (e) {
    status.style.color = '#ff8c42';
    status.textContent = `Request failed: ${e}`;
  }
}

// =============================================================================
// USERS PAGE
// Spec: docs/spec-per-bot-users-management-2026-05-29.md
//
// Consolidates pod admins, passphrases, per-bot owners, and per-bot paired
// users into a single page. Pod-wide content (admins, passphrases) is rendered
// by the existing loadIdentity() machinery — its element targets live inside
// page-users now. The bot rail and per-bot panel below are new.
// =============================================================================

// Sentinel id for the pod-wide "tile" — selecting it shows the pod
// pane (explainer + admins + passphrases) in lieu of a per-bot panel.
// Distinct from any real bot id (bots use lowercase letters/numbers).
const _USERS_POD_TILE = '__POD__';
let _usersActiveBot = _USERS_POD_TILE;
const _usersPendingCounts = {};  // bot_id → total pending count across channels
// Email surface toggle. Off by default; flipped via the per-bot panel's
// "Show emails" checkbox. Email rendering is conditional in the
// _usersRenderApprovedRow function. Persisted to localStorage so the
// operator's choice survives page reloads.
let _usersShowEmails = localStorage.getItem('evolve_users_show_emails') === '1';
const _usersChannelDataByBot = {};  // bot_id → last GET response (for re-render on toggle)

// ── Directory (Person cards) state — spec-user-directory-2026-06-22 §6 ──────
// The unified per-bot directory: admitted users AND address-book contacts
// (membership=None) in one list, filterable. Cached per bot so the
// Users/Contacts/All filter re-renders without a refetch.
let _usersDirFilter = 'all';  // 'all' | 'users' | 'contacts'
const _usersDirDataByBot = {};  // bot_id → last GET /directory response
// Channels Evolve can admit on — mirrors routes_bot_users.KNOWN_PROVIDERS. A
// contact is admittable only when it carries a messaging identity on one of
// these (admission is per-channel allowFrom); an email-only contact has none.
const _USERS_KNOWN_CHANNELS = ['telegram', 'slack', 'discord', 'whatsapp'];
// DOM-safe slug for element ids built from a "<platform>:<stable_id>" key
// (stable_id may be an email → contains @ and . which are invalid in ids).
const _dirSlug = (s) => String(s).replace(/[^a-zA-Z0-9]/g, '_');
// Escape a value for interpolation into a SINGLE-quoted JS string literal that
// itself lives inside a double-quoted onclick="" attribute. escHtml alone is
// NOT enough: it turns ' into &#39;, which the HTML parser decodes back to '
// BEFORE the JS string parser runs — re-opening the string break. So escape
// the backslash + quote for the JS layer first, then escHtml for the attribute.
// A directory stable_id can be an email (contains '), so this matters. Same
// hardening the page's _idIdChip / _idDropAdminPerson handlers already use.
const _dirArg = (s) =>
  escHtml(String(s == null ? '' : s).replace(/\\/g, '\\\\').replace(/'/g, "\\'"));
// Per-identity directory write URL (email / contact sub-resource).
const _dirUrl = (botId, platform, stableId, sub) =>
  `/api/admin/bots/${encodeURIComponent(botId)}/directory/` +
  `${encodeURIComponent(platform)}/${encodeURIComponent(stableId)}/${sub}`;

async function loadUsers() {
  // The pod-wide cards (admins, passphrases) reuse the identity API.
  // loadIdentity() caches the response on window.__usersIdentityData and
  // calls _usersRenderAfterIdentity() once data is available.
  try { await loadIdentity(); } catch (_) {}
}

function _usersRenderAfterIdentity(data) {
  _usersRenderBotTiles(data);
  // Skip scaffold-only residue (EVO-SEP-S4) — see isScaffoldOnlyBot in
  // pages/bot-detail.js — so a phantom `bots.evolve` never becomes a
  // selectable tile and the "fall back to POD" guard below sees only real bots.
  const allBots = data.bots || {};
  const bots = Object.keys(allBots).filter(id => !isScaffoldOnlyBot(allBots[id]));
  // Active tile may be POD (sentinel) or any known bot. If the operator
  // had previously selected a bot that no longer exists, fall back to
  // POD rather than silently switching to a different bot.
  if (_usersActiveBot !== _USERS_POD_TILE && !bots.includes(_usersActiveBot)) {
    _usersActiveBot = _USERS_POD_TILE;
  }
  _usersRenderActivePane();
}

// Swap which pane is visible (pod vs. per-bot) based on the current
// _usersActiveBot. Centralized so _usersSelectBot and the initial
// render share one path. The pod pane is HTML-static (its targets
// like #identity-pod-admin-state are populated by loadIdentity); the
// per-bot panel is JS-rendered on demand by _usersRenderBotPanel.
function _usersRenderActivePane() {
  const podPane = document.getElementById('users-pod-pane');
  const botPanel = document.getElementById('users-bot-panel');
  const isPod = (_usersActiveBot === _USERS_POD_TILE);
  if (podPane) podPane.style.display = isPod ? '' : 'none';
  if (botPanel) botPanel.style.display = isPod ? 'none' : '';
  if (!isPod) _usersRenderBotPanel(_usersActiveBot);
}

function _usersRenderBotTiles(data) {
  const rail = document.getElementById('users-bot-tiles');
  if (!rail) return;
  const bots = data.bots || {};
  // Sort: primary bot first (role === "primary"), then everything else
  // alphabetically by id. No multi/single grouping — operators asked
  // for a flat alpha list after the primary 2026-05-30. Scaffold-only
  // residue (EVO-SEP-S4, isScaffoldOnlyBot) is dropped first so a phantom
  // `bots.evolve` never renders a tile beside the real bots.
  const entries = Object.entries(bots)
    .filter(([, info]) => !isScaffoldOnlyBot(info))
    .sort(([a, ai], [b, bi]) => {
    const aPrimary = (ai && ai.role === 'primary') ? 0 : 1;
    const bPrimary = (bi && bi.role === 'primary') ? 0 : 1;
    if (aPrimary !== bPrimary) return aPrimary - bPrimary;
    return a < b ? -1 : a > b ? 1 : 0;
  });
  // POD tile is always rendered first — even if there are no bots, the
  // pod pane still hosts the admin claim + passphrase cards.
  const podActive = (_usersActiveBot === _USERS_POD_TILE);
  const podTile = `
    <div class="cm-bot-tile users-bot-tile${podActive ? ' active' : ''}" onclick="_usersSelectBot('${_USERS_POD_TILE}')">
      <div class="cm-bot-tile-header">
        <div class="cm-bot-tile-name">Pod</div>
      </div>
      <div style="font-size:0.7rem;color:var(--text2);margin-top:2px">pod-wide</div>
    </div>
  `;
  const botTiles = entries.map(([botId, info]) => {
    const isActive = botId === _usersActiveBot;
    const pending = _usersPendingCounts[botId] || 0;
    const pendingChip = pending > 0
      ? `<span class="users-pending-chip">●${pending}</span>`
      : '';
    const kind = info.multi_user ? 'multi-user' : 'single-user';
    // ``users-bot-tile`` is a Users-page-specific narrowing of cm-bot-tile
    // — keeps the existing tile look but with a smaller min-width so the
    // rail packs more bots per row. CSS rule lives near .cm-bot-tile.
    return `
      <div class="cm-bot-tile users-bot-tile${isActive ? ' active' : ''}" onclick="_usersSelectBot('${escHtml(botId)}')">
        <div class="cm-bot-tile-header">
          <div class="cm-bot-tile-name">${escHtml(botLabel(botId))}</div>
          ${pendingChip}
        </div>
        <div style="font-size:0.7rem;color:var(--text2);margin-top:2px">${kind}</div>
      </div>
    `;
  }).join('');
  rail.innerHTML = podTile + botTiles;
}

function _usersSelectBot(botId) {
  _usersActiveBot = botId;
  if (window.__usersIdentityData) _usersRenderBotTiles(window.__usersIdentityData);
  _usersRenderActivePane();
}

function _usersRenderBotPanel(botId) {
  const panel = document.getElementById('users-bot-panel');
  if (!panel) return;
  const data = window.__usersIdentityData || {};
  const info = (data.bots || {})[botId];
  if (!info) {
    panel.innerHTML = '<div class="card" style="margin-top:14px"><div class="empty">Bot not found.</div></div>';
    return;
  }
  const cache = (data.pod_admins || {}).resolved_names || {};
  const kind = info.multi_user ? 'multi-user' : 'single-user';

  panel.innerHTML = `
    <div class="card" style="margin-top:14px">
      <div class="card-title">${escHtml(botLabel(botId))} <span class="users-mode-toggle" title="Switch this bot between single-user and multi-user mode. Multi-user partitions per-user signals and gates DMs by pod-admin/owner status; single-user trusts any DM and collapses alerts to the primary.">
        <button class="${info.multi_user ? '' : 'active'}" onclick="_usersSetMultiUser('${escHtml(botId)}', false, ${info.multi_user ? 'true' : 'false'})">Single-user</button>
        <button class="${info.multi_user ? 'active' : ''}" onclick="_usersSetMultiUser('${escHtml(botId)}', true, ${info.multi_user ? 'true' : 'false'})">Multi-user</button>
      </span></div>

      <div style="margin-top:6px;font-size:0.78rem;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:0.06em">Owner / primary user</div>
      <div id="users-owner-${escHtml(botId)}">
        ${_idRenderBotCard(botId, info, cache)}
      </div>

      <div style="margin-top:18px;font-size:0.78rem;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:0.06em">Reachable on</div>
      <div id="users-personlink-${escHtml(botId)}" style="margin-top:8px">
        <div class="loading"><div class="spinner"></div> Loading platforms…</div>
      </div>

      <div style="display:flex;align-items:center;justify-content:space-between;margin-top:18px">
        <div style="font-size:0.78rem;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:0.06em">Users by channel</div>
        <label title="Show email addresses where the channel exposes them (currently Slack only, when the bot app has users:read.email scope)" style="font-size:0.74rem;color:var(--text2);cursor:pointer;display:inline-flex;align-items:center;gap:5px;user-select:none">
          <input type="checkbox" ${_usersShowEmails ? 'checked' : ''} onchange="_usersToggleEmails()" style="vertical-align:middle;cursor:pointer"> Show emails
        </label>
      </div>
      <div id="users-by-channel-${escHtml(botId)}" style="margin-top:8px">
        <div class="loading"><div class="spinner"></div> Loading paired users…</div>
      </div>

      <div style="margin-top:18px;font-size:0.78rem;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:0.06em"
           title="Standing per-user model-tier defaults for this bot's conversations — what each user set with the 'evo tier-default' chat command. Applied above the bot-wide Conversations default in routing precedence. Read-only here; users change it from the chat thread.">Per-user tier defaults</div>
      <div id="users-tier-prefs-${escHtml(botId)}" style="margin-top:8px">
        <div class="loading"><div class="spinner"></div> Loading tier defaults…</div>
      </div>
      ${_idRenderAliasSection(botId)}
    </div>

    <div class="card" style="margin-top:14px" id="users-directory-card-${escHtml(botId)}">
      <div class="card-title">Directory</div>
      <div style="font-size:0.74rem;color:var(--text2);margin:2px 0 8px">
        Everyone this bot knows — admitted <strong>Users</strong> and address-book
        <strong>Contacts</strong> (people it acts toward but who never message it).
        Edit emails and contact details here; provenance badges show whether you,
        the bot, or the channel supplied each fact.
      </div>
      <div id="users-directory-${escHtml(botId)}">
        <div class="loading"><div class="spinner"></div> Loading directory…</div>
      </div>
    </div>
  `;

  _usersFetchAndRenderByChannel(botId);
  _usersFetchAndRenderDirectory(botId);
  _usersLoadPersonLink(botId);
  // Phase C.5 — kick off the email-alias load for this bot. The card
  // renders an "Email alias … Loading…" placeholder; without this
  // call nothing ever resolved it on the Users-tab path (only the
  // Identity-tab _idRenderBots flow loaded it). Same pattern as
  // _idRenderBots' parallel kickoff at line ~55032.
  if (typeof _idLoadAliasForBot === 'function') {
    _idLoadAliasForBot(botId);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
//  "Same person" — one human, two platforms (M1-B4a)
// ═══════════════════════════════════════════════════════════════════════════
//
// The operator surface over roster_identity's D1 seam (spec
// docs/spec-users-meta-2026-06-15.md §M1, invariants 1/2/6/7). A Discord
// snowflake and a Telegram user id share nothing, so Evolve cannot infer that
// two ids are one human — and must not try, because roles attach to admitted
// identities and a wrong link is a privilege transfer. The link is therefore
// an operator ASSERTION, and this is where it gets made.
//
// It lives directly under "Owner / primary user" on the per-bot panel because
// that is the row being asserted about: the section above says who the owner
// is, this one says where that same person can be reached. The backend
// (routes_person_link) owns validation, the collision refusal, and the
// one-id-per-channel limit; nothing here re-derives them.
//
// Three things the operator sees:
//   1. Reachable on — the row's external_ids, now multi-valued (M1-B2).
//   2. Link a platform — channel + id → POST .../person-link/link.
//   3. Unknown to Evolve — the roster_coherence monitor's
//      roster_oc_identity_unknown Signal, which fires at exactly the moment
//      the "same person or new person?" question arises.
//
// force is NEVER sent on the first attempt. A 409 conflict opens a modal that
// names the other row and states that confirming APPENDS (the other row keeps
// its id); only that explicit confirm re-POSTs with force:true.

const _usersPersonLinkByBot = {};   // bot_id → last GET .../person-link

// The scope limit, said once in operator words. The server carries its own
// (longer) statement of the same rule in `one_id_per_channel_reason`, which is
// what a refused POST returns; this is the ambient form-side note. Both go
// away together when the plugin deploy unblocks a second id per channel — see
// routes_person_link._CHANNEL_OCCUPIED_HINT for the extension point.
const _USERS_ONE_ID_NOTE =
  'A second id on a platform this person already uses is not offered yet: bots ' +
  'still resolve roles from a stringified id, and two ids on one platform ' +
  'collapse into one bogus id.';

async function _usersLoadPersonLink(botId) {
  const el = document.getElementById(`users-personlink-${botId}`);
  if (!el) return;
  try {
    const r = await fetch(
      `/api/admin/bots/${encodeURIComponent(botId)}/person-link`,
      { cache: 'no-store' });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || !data.ok) {
      const msg = (data && data.message) || `HTTP ${r.status}`;
      el.innerHTML = `<div class="empty" style="font-size:0.8rem;padding:6px 0">Could not load platforms: ${escHtml(msg)}</div>`;
      return;
    }
    _usersPersonLinkByBot[botId] = data;
    _usersRenderPersonLink(el, botId, data);
    _usersLoadCoherenceGaps(botId);
  } catch (e) {
    el.innerHTML = `<div class="empty" style="font-size:0.8rem;padding:6px 0">Could not load platforms: ${escHtml(String(e))}</div>`;
  }
}

function _usersRenderPersonLink(el, botId, data) {
  const cache = ((window.__usersIdentityData || {}).pod_admins || {}).resolved_names || {};
  const reachable = data.reachable || [];
  const linkable = data.linkable_channels || [];

  const chips = reachable.map(row => {
    const ids = (row.ids || []).map(id => {
      const chip = _idIdChip(row.channel, id, cache);
      const args = `'${escHtml(botId)}','${escHtml(row.channel)}',${attrJsLiteral(String(id))}`;
      return `<span title="${chip.title}" style="display:inline-flex;align-items:center;gap:6px;background:var(--bg2);padding:4px 8px;border-radius:3px;font-size:0.78rem">
          <span style="color:var(--text3)">${escHtml(row.label || row.channel)}:</span>${chip.label}
          <button class="btn btn-sm btn-warning" onclick="_usersPersonUnlink(${args})" title="Remove this id from this person. Only touches this row." style="padding:1px 8px;font-size:0.7rem">Unlink</button>
        </span>`;
    }).join(' ');
    return ids;
  }).join(' ');

  const reachHtml = reachable.length
    ? `<div style="display:flex;flex-wrap:wrap;gap:6px">${chips}</div>`
    : `<div style="font-size:0.78rem;color:var(--text2)">No platform ids recorded for this person yet.</div>`;

  let formHtml;
  if (!data.row_exists) {
    formHtml = `<div style="font-size:0.76rem;color:var(--text2);margin-top:8px">
        Set an owner above first — linking attaches a platform id to an <em>existing</em> person, it never creates one.
      </div>`;
  } else if (!linkable.length) {
    formHtml = `<div style="font-size:0.76rem;color:var(--text2);margin-top:8px">
        This person already has an id on every platform Evolve can link.
        ${escHtml(_USERS_ONE_ID_NOTE)}
      </div>`;
  } else {
    const options = linkable.map(c =>
      `<option value="${escHtml(c.channel)}">${escHtml(c.label)}</option>`).join('');
    const firstHint = linkable[0] || {};
    formHtml = `
      <div style="margin-top:8px">
        <button class="btn btn-sm btn-primary" onclick="_usersTogglePersonLinkForm('${escHtml(botId)}')" style="padding:3px 10px;font-size:0.76rem">Link another platform</button>
      </div>
      <div id="users-personlink-form-${escHtml(botId)}" style="display:none;padding:10px 12px;background:var(--bg2);border-radius:6px;margin-top:8px">
        <div style="font-size:0.76rem;color:var(--text2);margin-bottom:8px">
          Assert that an id on another platform is <strong>this same person</strong>. Evolve can never work this out on its own —
          two platform ids share no signal that links them, and roles follow admitted identities, so this has to be your call.
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:end">
          <div>
            <label for="users-plink-ch-${escHtml(botId)}" style="display:block;font-size:0.72rem;color:var(--text2);margin-bottom:3px">Platform</label>
            <select id="users-plink-ch-${escHtml(botId)}" class="form-select input-w-md" onchange="_usersPersonLinkHint('${escHtml(botId)}')">${options}</select>
          </div>
          <div>
            <label for="users-plink-eid-${escHtml(botId)}" style="display:block;font-size:0.72rem;color:var(--text2);margin-bottom:3px">${escHtml(firstHint.id_label || 'External ID')}</label>
            <input id="users-plink-eid-${escHtml(botId)}" class="input-w-md" type="text" placeholder="e.g. 123456789">
          </div>
          <button class="btn btn-sm btn-primary" onclick="_usersPersonLinkSubmit('${escHtml(botId)}')">Same person</button>
        </div>
        <div id="users-plink-hint-${escHtml(botId)}" style="font-size:0.72rem;color:var(--text2);margin-top:6px">${escHtml(firstHint.id_hint || '')}</div>
        <div style="font-size:0.72rem;color:var(--text3);margin-top:6px">
          Only platforms this person has no id on yet are offered. ${escHtml(_USERS_ONE_ID_NOTE)}
        </div>
      </div>`;
  }

  el.innerHTML = `
    <div style="font-size:0.76rem;color:var(--text2);margin-bottom:6px">
      One human, one row. Every platform id below reaches the same person — that is what lets a known
      owner turn up on a second platform without becoming a second user.
    </div>
    ${reachHtml}
    ${formHtml}
    <div id="users-personlink-gaps-${escHtml(botId)}" style="margin-top:10px"></div>`;
}

function _usersTogglePersonLinkForm(botId) {
  const form = document.getElementById(`users-personlink-form-${botId}`);
  if (!form) return;
  form.style.display = (form.style.display === 'none') ? '' : 'none';
}

// Swap the id label + format hint when the operator picks a different
// platform. Both strings come from channel_registry via the GET payload —
// no channel table lives in this file (invariant 7).
function _usersPersonLinkHint(botId) {
  const data = _usersPersonLinkByBot[botId] || {};
  const sel = document.getElementById(`users-plink-ch-${botId}`);
  const hint = document.getElementById(`users-plink-hint-${botId}`);
  const input = document.getElementById(`users-plink-eid-${botId}`);
  if (!sel) return;
  const spec = (data.linkable_channels || []).find(c => c.channel === sel.value) || {};
  if (hint) hint.textContent = spec.id_hint || '';
  const label = input && input.previousElementSibling;
  if (label && label.tagName === 'LABEL') label.textContent = spec.id_label || 'External ID';
}

async function _usersPersonLinkSubmit(botId) {
  const sel = document.getElementById(`users-plink-ch-${botId}`);
  const input = document.getElementById(`users-plink-eid-${botId}`);
  if (!sel || !input) return;
  const externalId = (input.value || '').trim();
  if (!externalId) { toast('Enter the platform id first', 'warn'); return; }
  await _usersPersonLinkPost(botId, sel.value, externalId, false);
}

// POST the link. `force` is a PARAMETER, never a default: the only caller that
// passes true is the confirm handler below, after the operator has read the
// named collision. A raw fetch (not api()) so an expected 409 doesn't land in
// the error log — a collision is a decision to make, not a failure.
async function _usersPersonLinkPost(botId, channel, externalId, force) {
  let r, data;
  try {
    r = await fetch(
      `/api/admin/bots/${encodeURIComponent(botId)}/person-link/link`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel, external_id: externalId, force: !!force }),
      });
    data = await r.json().catch(() => ({}));
  } catch (e) {
    toast(`Link failed: ${e}`, 'err');
    return;
  }
  if (r.ok && data.ok) {
    toast(force ? 'Linked (appended — the other person keeps their id)' : 'Linked', 'ok');
    _usersLoadPersonLink(botId);
    loadIdentity();
    return;
  }
  if (r.status === 409 && data.error === 'conflict') {
    await _usersPersonLinkConfirmConflict(botId, channel, externalId, data);
    return;
  }
  toast(`Link refused: ${data.message || `HTTP ${r.status}`}`, 'err');
}

// The collision, presented as the real decision it is.
//
// Another row already records this id. That does NOT prove a different human
// (one operator is often a pod admin AND several bots' owner), and it does not
// prove the same one either — so the seam refuses and we ask. Two things the
// copy must not soften: WHO already holds it, and that confirming APPENDS
// rather than moves. The other row keeps its record; removing it there is a
// separate, explicit Unlink.
async function _usersPersonLinkConfirmConflict(botId, channel, externalId, data) {
  const holders = (data.conflicts || []).map(c => `• ${c.label}`).join('\n');
  const podAdminBag = (data.conflicts || []).some(c => c.is_person === false);
  const body =
    `${channel}:${externalId} is already recorded on:\n\n${holders}\n\n` +
    'Evolve cannot tell whether that is the same human. If it is, confirming ' +
    'ADDS the id to this person as well — it does not move it. ' +
    (podAdminBag
      ? 'Note: one of those is the pod-admin identity bag, not a person; an id can legitimately be in it and be an owner too.\n\n'
      : '') +
    'The other row keeps its record; to remove it there, use that row\'s Unlink.';
  const ok = await confirmModal({
    title: 'That id already belongs to someone',
    body,
    confirmLabel: 'Yes — same person, add it here too',
    cancelLabel: 'Cancel',
    danger: true,
  });
  if (!ok) return;
  await _usersPersonLinkPost(botId, channel, externalId, true);
}

async function _usersPersonUnlink(botId, channel, externalId) {
  const ok = await confirmModal({
    title: 'Unlink this platform id?',
    body: `${channel}:${externalId} will no longer resolve to this person. ` +
          'Only this row is touched — any other row that records the same id keeps it.',
    confirmLabel: 'Unlink',
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await fetch(
      `/api/admin/bots/${encodeURIComponent(botId)}/person-link/unlink`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel, external_id: externalId }),
      });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || !data.ok) {
      toast(`Unlink failed: ${data.message || `HTTP ${r.status}`}`, 'err');
      return;
    }
    toast(data.removed ? 'Unlinked' : 'Nothing to unlink', data.removed ? 'ok' : 'warn');
    _usersLoadPersonLink(botId);
    loadIdentity();
  } catch (e) {
    toast(`Unlink failed: ${e}`, 'err');
  }
}

// ── The feed: roster↔OC coherence gaps ─────────────────────────────────────
//
// roster_coherence (M1-B3) fires `roster_oc_identity_unknown` for identities a
// bot's openclaw.json names that no Evolve roster source knows. That is
// precisely the moment the operator decides "same person" vs "new person", so
// the Signal's unknown ids are surfaced right beside the link affordance
// instead of only on the Alerts page. Read-only consumption of the existing
// /api/signals list — no new producer, no signal writes.
//
// An id on a channel the person already has an id on is shown WITHOUT a link
// button: the one-id-per-channel limit is real (see routes_person_link), and
// offering a button the server would refuse would be a lie.
async function _usersLoadCoherenceGaps(botId) {
  const el = document.getElementById(`users-personlink-gaps-${botId}`);
  if (!el) return;
  let sigs;
  try {
    const r = await fetch(
      `/api/signals?producer=roster_coherence&bot_id=${encodeURIComponent(botId)}&state=firing`,
      { cache: 'no-store' });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || !data.ok) return;   // best-effort: the feed is a bonus lane
    sigs = (data.signals || []).filter(
      s => s && s.type === 'roster_oc_identity_unknown');
  } catch (_) {
    return;
  }
  if (!sigs.length) { el.innerHTML = ''; return; }
  _usersRenderCoherenceGaps(el, botId, sigs);
}

function _usersRenderCoherenceGaps(el, botId, sigs) {
  const state = _usersPersonLinkByBot[botId] || {};
  const occupied = new Set((state.reachable || []).map(r => r.channel));
  const linkable = new Set((state.linkable_channels || []).map(c => c.channel));
  const rows = [];
  for (const sig of sigs) {
    const details = sig.details || {};
    const channel = details.channel || '';
    const ids = Array.isArray(details.unknown_identities) ? details.unknown_identities : [];
    for (const id of ids) {
      let action;
      if (!state.row_exists) {
        action = '<span style="font-size:0.72rem;color:var(--text2)">set an owner above first</span>';
      } else if (occupied.has(channel)) {
        action = `<span style="font-size:0.72rem;color:var(--text2)" title="A second id on one platform is not offered yet — see the note above">owner already has a ${escHtml(channel)} id</span>`;
      } else if (!linkable.has(channel)) {
        action = '<span style="font-size:0.72rem;color:var(--text2)">not linkable</span>';
      } else {
        const args = `'${escHtml(botId)}','${escHtml(channel)}',${attrJsLiteral(String(id))}`;
        action = `<button class="btn btn-sm" onclick="_usersPersonLinkPost(${args}, false)" title="Assert this is the same human as this bot's owner" style="padding:1px 8px;font-size:0.72rem">Same person as the owner</button>`;
      }
      rows.push(`<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:3px 0">
          <span style="font-size:0.78rem"><span style="color:var(--text3)">${escHtml(channel)}:</span> <code style="background:var(--bg2);padding:2px 6px;border-radius:3px">${escHtml(String(id))}</code></span>
          ${action}
        </div>`);
    }
  }
  if (!rows.length) { el.innerHTML = ''; return; }
  el.innerHTML = `
    <div style="border:1px solid var(--border);border-radius:6px;padding:10px 12px">
      <div style="font-size:0.76rem;font-weight:600;margin-bottom:2px">Unknown to Evolve</div>
      <div style="font-size:0.74rem;color:var(--text2);margin-bottom:6px">
        This bot's config lets these ids reach it, but no Evolve roster knows them. If one is your
        owner on another platform, link it here. Otherwise admit them below as their own person, or
        remove the id from the bot's config.
      </div>
      ${rows.join('')}
    </div>`;
}

window._usersTogglePersonLinkForm = _usersTogglePersonLinkForm;
window._usersPersonLinkHint = _usersPersonLinkHint;
window._usersPersonLinkSubmit = _usersPersonLinkSubmit;
window._usersPersonLinkPost = _usersPersonLinkPost;
window._usersPersonUnlink = _usersPersonUnlink;

async function _usersFetchAndRenderByChannel(botId) {
  const target = document.getElementById(`users-by-channel-${botId}`);
  if (!target) return;
  try {
    const data = await api('GET', `/api/admin/bots/${encodeURIComponent(botId)}/users`);
    if (!data || data.error || !data.by_channel) {
      const msg = (data && data.error) ? data.error : 'no by_channel in response';
      target.innerHTML = `<div style="color:#ff8c42;font-size:0.85rem">Failed to load paired users: ${escHtml(msg)} <span style="color:var(--text2)">(check the admin-ui daemon is running the latest code; <code>ssh pod_admin_user@mini sudo /bin/launchctl kickstart -k system/ai.evolve.evolve.admin-ui</code> after a deploy).</span></div>`;
      _usersFetchAndRenderTierPrefs(botId);  // G5 — still render (names degrade to raw keys)
      return;
    }
    _usersChannelDataByBot[botId] = data;
    _usersRenderByChannel(target, botId, data);
    let pending = 0;
    Object.values(data.by_channel || {}).forEach(ch => { pending += (ch.pending || []).length; });
    _usersPendingCounts[botId] = pending;
    _usersRefreshPendingChips();
  } catch (e) {
    target.innerHTML = `<div style="color:#ff8c42;font-size:0.85rem">Failed to load paired users: ${escHtml(String(e))}</div>`;
  }
  // G5 — per-user tier defaults. Kicked off AFTER the by_channel render
  // (success or failure) so _usersChannelDataByBot[botId] is settled and
  // the tier-prefs renderer can join user keys to display names without
  // racing the identity fetch.
  _usersFetchAndRenderTierPrefs(botId);
}

function _usersRenderByChannel(el, botId, data) {
  const byCh = data.by_channel || {};
  const channels = Object.keys(byCh);
  // H.2 — pull the bot's primary_passphrase from the cached Identity
  // data (loaded by Identity tab + Users tab on entry) so the channel
  // block can show it inline when the newcomer mode is
  // admit_with_passphrase. Avoids a backend change to the
  // /users endpoint — the data is already on the page.
  const idData = window.__usersIdentityData || {};
  const botInfo = (idData.bots || {})[botId] || {};
  const botPassphrase = botInfo.primary_passphrase_override || '';
  const rendered = channels
    .map(ch => {
      const chData = byCh[ch] || {};
      chData.passphrase = botPassphrase;  // injected for renderer
      return _usersRenderChannelBlock(botId, ch, chData);
    })
    .filter(s => s);  // drop hidden channels (unsupported, no users, no pending)
  if (!rendered.length) {
    el.innerHTML = '<div class="empty" style="font-size:0.85rem;padding:8px 0">No channels configured for this bot. Configure messaging in <a href="javascript:void(0)" onclick="document.querySelector(\'.nav-item[data-page=&quot;integrations-keys&quot;]\').click()" style="color:var(--accent)">Plugins</a> first.</div>';
    return;
  }
  // Per spec 2026-06-07, the top-level ``blocked`` array surfaces
  // sticky-deny identities below the per-channel sections so the
  // operator can see and unblock them without scanning approved[].
  // Hidden entirely when nobody is blocked — most pods will never
  // populate this.
  el.innerHTML = rendered.join('') + _usersRenderBlockedSection(botId, data.blocked || []);
}

function _usersRenderChannelBlock(botId, channel, chData) {
  const approved = chData.approved || [];
  const pending = chData.pending || [];
  // R1a (2026-06-17) — the config-level group/channel allowlist
  // (openclaw.json channels.<ch>.allowFrom under groupPolicy:allowlist).
  // A SEPARATE OpenClaw gate from the DM pairing store `approved` reads;
  // surfaced as its own list so group-authorized users no longer read as
  // "not admitted" and the operator can see who can drive the bot in
  // channels.
  const groupAccess = chData.group_access || [];
  // R1a PR2 — True when this channel is group-allowlist-gated even if the
  // effective list is empty; lets us show the management UI (add-by-id) so the
  // operator can authorize the FIRST group member on a gated-but-empty channel.
  const groupGated = !!chData.group_allowlist_gated;
  // Phase F.2 — turn-history users who aren't in approved or pending.
  // Surfaces users who messaged the bot but never went through /start
  // pairing (e.g. Slack DMs where the bot is in a permissive mode).
  const seenRecently = chData.seen_recently || [];  // "Active · not admitted"
  // Hide channels the bot doesn't have configured at all (no creds
  // files on disk → supported=false AND no allowlist AND no pending
  // AND no group access AND no recent activity). Without this filter the
  // page renders a "no users paired" row for every KNOWN_PROVIDER even on
  // bots that only use one channel, which confused operators 2026-05-29.
  if (!chData.supported && !approved.length && !groupAccess.length
      && !groupGated && !pending.length && !seenRecently.length) {
    return '';
  }
  const approvedRows = approved.length
    ? approved.map(u => _usersRenderApprovedRow(botId, channel, u)).join('')
    : '<div style="font-size:0.82rem;color:var(--text2);padding:4px 0">No users approved yet.</div>';
  // R1a — "Channel access (group)" list. Rendered when the channel is
  // allowlist-gated (groupPolicy:allowlist) OR already has members. PR2
  // (2026-06-18) makes it MANAGEABLE: each row gets a Revoke action and the
  // section gets an add-by-id box (shown even on a gated-but-empty channel, so
  // the first member can be authorized). Writes go ONLY to openclaw.json's
  // group allowlist — never the DM pairing store — so this list and
  // "Approved · DM" stay strictly separate.
  const groupRows = groupAccess.length
    ? groupAccess.map(u => _usersRenderGroupRow(botId, channel, u)).join('')
    : '<div style="font-size:0.72rem;color:var(--text3);padding:4px 0">No group members yet — add one below to authorize channel/group access.</div>';
  const groupSection = (groupAccess.length || groupGated)
    ? `
      <div style="margin-top:10px">
        <div style="font-size:0.74rem;color:var(--text2);text-transform:uppercase;letter-spacing:0.06em"
             title="Authorized to use this bot in group/channel contexts via openclaw.json's group allowlist (channels.${escHtml(channel)}.allowFrom under groupPolicy:allowlist). A SEPARATE gate from DM approval — the two lists can differ.">Channel access · group (${groupAccess.length})</div>
        <div style="font-size:0.72rem;color:var(--text3);margin:3px 0 5px">Authorized in channels/groups by the config allowlist OpenClaw enforces per-sender. Distinct from DM approval — approving or revoking here changes only the channel allowlist, never DM access.</div>
        <div>${groupRows}</div>
        ${_usersRenderGroupAddRow(botId, channel)}
      </div>
    `
    : '';
  const pendingRows = pending.length
    ? pending.map(req => _usersRenderPendingRow(botId, channel, req)).join('')
    : '<div style="font-size:0.82rem;color:var(--text2);padding:4px 0">Pending — none</div>';
  // "Active · not admitted" — identities that messaged the bot in the
  // last 30 days but aren't approved or pending (often group or DM
  // senders who never ran /start). Given a divider + helper line so it
  // reads as a distinct triage section below Pending rather than a
  // buried footnote. Omitted entirely (not just empty) when there's
  // nothing — keeps the channel card tight for normal cases.
  const seenSection = seenRecently.length
    ? `
      <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--border)">
        <div style="font-size:0.74rem;color:var(--text2);text-transform:uppercase;letter-spacing:0.06em"
             title="Identities that messaged this bot in the last 30 days but aren't approved or pending. Often group or DM senders who never ran /start.">Active · not admitted (${seenRecently.length})</div>
        <div style="font-size:0.72rem;color:var(--text3);margin:3px 0 5px">Messaged this bot recently but isn't approved or pending. Admit to add them as a participant, or Ignore to dismiss the row.</div>
        <div>${seenRecently.map(s => _usersRenderSeenRow(botId, channel, s)).join('')}</div>
      </div>
    `
    : '';
  // Per-channel newcomer mode selector (spec 2026-06-07 §11). Three
  // modes: auto_admit (group membership = approved), require_approval
  // (existing pairing flow — the default), closed (silent ignore
  // /start). The current value comes from the overlay's channels
  // block; the GET response always carries it.
  const newcomerMode = chData.newcomer_mode || 'require_approval';
  const modeSelector = _usersRenderNewcomerModeSelector(botId, channel, newcomerMode);
  // H.2 — when the newcomer mode is admit_with_passphrase, expose
  // the bot-level passphrase inline so the operator can set/view/
  // update it without leaving the channel block. Bot-level scope is
  // intentional for v1 (same value shared across all channels using
  // passphrase mode); per-channel passphrase is a future enhancement.
  const passphraseRow = newcomerMode === 'admit_with_passphrase'
    ? _usersRenderPassphraseRow(botId, chData.passphrase)
    : '';
  return `
    <div style="margin-bottom:14px;padding:10px 12px;border:1px solid var(--border);border-radius:6px;background:var(--bg2)">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;gap:10px;flex-wrap:wrap">
        <div style="font-size:0.85rem;font-weight:600">${escHtml(_channelDisplayName(channel))}</div>
        ${modeSelector}
      </div>
      ${passphraseRow}
      <div style="font-size:0.74rem;color:var(--text2);text-transform:uppercase;letter-spacing:0.06em;margin-top:8px"
           title="Approved for DIRECT MESSAGES via the credentials pairing store (dmPolicy:pairing). Distinct from the channel/group allowlist below.">Approved · DM (${approved.length})</div>
      <div>${approvedRows}</div>
      ${groupSection}
      <div style="font-size:0.74rem;color:var(--text2);text-transform:uppercase;letter-spacing:0.06em;margin-top:10px">Pending (${pending.length})</div>
      <div>${pendingRows}</div>
      ${seenSection}
    </div>
  `;
}

// Row renderer for the "Active · not admitted" section. Shares the
// .users-row table layout from approved rows so columns align. Triage
// actions: Admit (force-add to allowFrom, skipping /start), Block
// (sticky deny), Ignore (dismiss the row without changing access).
function _usersRenderSeenRow(botId, channel, s) {
  const name = s.display_name
    ? escHtml(s.display_name)
    : (s.username ? `@${escHtml(s.username)}`
                  : '<span style="color:var(--text2)">[unknown]</span>');
  // F.1 stamps via_channel when we rewrote a Slack D-id to a U-id.
  // Subtle hint in the id cell title so operators can spot DM-derived
  // rows when debugging.
  const idTitle = s.via_channel
    ? `Originally observed via DM channel ${s.via_channel}; rewritten to user id.`
    : '';
  const idCell = `<code class="users-row-id" title="${escHtml(idTitle)}">${escHtml(s.id)}</code>`;
  const turnsBadge = (s.turn_count && s.turn_count > 0)
    ? `<span style="font-size:0.62rem;color:var(--text2);margin-left:4px" title="${s.turn_count} turn(s) in the last 30 days">·${s.turn_count}</span>`
    : '';
  const lastCell = s.last_seen
    ? `<span class="users-row-lastseen" title="Last seen: ${escHtml(s.last_seen)}">${escHtml(_usersRelativeTime(s.last_seen))}${turnsBadge}</span>`
    : `<span class="users-row-lastseen" style="color:var(--text3)">—</span>`;
  return `
    <div class="users-row">
      <span class="users-row-name">${name}</span>
      ${idCell}
      <span class="users-row-surfaces" title="No engagement surface set — these users haven't been admitted yet">—</span>
      ${lastCell}
      ${_usersShowEmails ? '<span class="users-row-email"></span>' : ''}
      <span class="users-role-locked" style="font-style:italic" title="Not yet paired — click Approve to admit this user as a participant.">Unpaired</span>
      <span class="users-row-actions">
        <button class="btn btn-sm btn-green" onclick="_usersApproveSeen('${escHtml(botId)}','${escHtml(channel)}','${escHtml(s.id)}','${escHtml(s.display_name || s.username || '')}')" title="Add to allowFrom as a participant. Skips the /start pairing flow.">Admit</button>
        <button class="btn btn-sm btn-danger" onclick="_usersBlockUser('${escHtml(botId)}','${escHtml(channel)}','${escHtml(s.id)}')" title="Sticky block — add to overlay block index so they can't re-pair.">Block</button>
        <button class="btn btn-sm" onclick="_usersIgnoreSeen('${escHtml(botId)}','${escHtml(channel)}','${escHtml(s.id)}')" title="Dismiss this row from the list. Doesn't block them or change their access — just hides them here.">Ignore</button>
      </span>
    </div>
  `;
}

// H.2 — inline passphrase row shown in the channel block when the
// newcomer mode is admit_with_passphrase. Bot-level value (shared
// across all channels using this mode); edit-in-place writes to the
// existing per-bot passphrase endpoint.
function _usersRenderPassphraseRow(botId, currentValue) {
  const has = !!currentValue;
  // The element ids match the standalone passphrase row that lived
  // in _idRenderBotCard before H.2 — so _idEditBotPassphrase /
  // _idSaveBotPassphrase / loadIdentity still drive this row without
  // modification.
  const display = has
    ? `<code id="id-bot-pass-val-${escHtml(botId)}" style="background:var(--bg);padding:3px 8px;border-radius:3px;font-size:0.78rem">${escHtml(currentValue)}</code>`
    : `<code id="id-bot-pass-val-${escHtml(botId)}" style="background:var(--bg);padding:3px 8px;border-radius:3px;font-size:0.78rem;color:var(--text2)"><em>not set — admit-with-passphrase will fall through to require-approval</em></code>`;
  return `
    <div style="display:flex;align-items:center;gap:8px;margin-top:6px;padding:8px 10px;background:var(--bg);border-radius:4px;font-size:0.78rem;flex-wrap:wrap">
      <span style="color:var(--text2);min-width:90px">Passphrase:</span>
      ${display}
      <button class="btn btn-sm" onclick="_idEditBotPassphrase('${escHtml(botId)}')" style="padding:2px 8px;font-size:0.72rem">${has ? 'Edit' : 'Set'}</button>
      <span id="id-bot-pass-status-${escHtml(botId)}" class="subtle" style="font-size:0.72rem"></span>
      <span style="color:var(--text3);font-size:0.7rem;margin-left:auto">Bot-level — shared across all channels using this mode.</span>
    </div>`;
}

function _usersRenderNewcomerModeSelector(botId, channel, currentMode) {
  // Compact inline select. Labels chosen to be self-explanatory at
  // glance — operators don't need to know the wire-format string
  // ('auto_admit') to pick the option.
  // H.2 — added admit_with_passphrase. When that mode is active the
  // bot's primary_passphrase is the gate: newcomer messages
  // containing the phrase auto-admit; otherwise require_approval
  // semantics apply.
  const opts = [
    { v: 'auto_admit', label: 'Auto-admit', tip: 'Anyone in the group is admitted as a participant. Removal from the group revokes access.' },
    { v: 'admit_with_passphrase', label: 'Admit with passphrase', tip: 'Newcomers whose message contains the bot\'s passphrase are auto-admitted; everyone else falls through to require-approval.' },
    { v: 'require_approval', label: 'Require approval', tip: 'Existing behavior — /start returns a code, admin approves here.' },
    { v: 'closed', label: 'Closed', tip: 'Silent ignore. Roster is managed only by the operator.' },
  ];
  const optsHtml = opts.map(o =>
    `<option value="${o.v}" ${o.v === currentMode ? 'selected' : ''} title="${escHtml(o.tip)}">${o.label}</option>`
  ).join('');
  return `
    <label style="display:inline-flex;align-items:center;gap:6px;font-size:0.74rem;color:var(--text2)">
      Newcomers:
      <select class="form-select input-w-auto" onchange="_usersSetNewcomerMode('${escHtml(botId)}','${escHtml(channel)}', this.value)"
              style="font-size:0.78rem;padding:2px 6px">
        ${optsHtml}
      </select>
    </label>
  `;
}

function _usersRenderApprovedRow(botId, channel, u) {
  const name = u.display_name
    ? escHtml(u.display_name)
    : '<span style="color:var(--text2)">[unknown]</span>';
  // Secondary @handle — shown when the platform username differs from
  // the display name (e.g. Discord global_name vs @username, Telegram
  // first+last vs @handle). Omitted when absent or redundant.
  const handle = (u.username && u.username !== u.display_name)
    ? `<span style="color:var(--text2);font-size:0.78rem;margin-left:5px">@${escHtml(u.username)}</span>`
    : '';
  // Descriptive labels (pod_admin / owner) from network.json claims —
  // distinct from the operative role (admin / primary_user / participant
  // / blocked). Render owner-and-pod-admin as small inline chips next
  // to the name so the operator sees identity context without clutter.
  const labels = (u.labels || []).map(l =>
    `<span class="badge badge-muted" style="font-size:0.62rem;margin-left:4px" title="${escHtml(l)} (from network.json identity)">${escHtml(l)}</span>`
  ).join('');
  // Engagement surfaces: render as a compact ALL-CAPS string
  // (GROUP / DM / GROUP·DM) instead of separate chips — cheaper visual
  // weight, fits in a fixed column.
  const surfaces = u.engagement_surfaces || [];
  const surfacesText = surfaces.length
    ? surfaces.map(s => escHtml(s)).join('·')
    : '—';
  // Email column — only rendered when the page-level "Show emails"
  // toggle is on. Empty span keeps column width stable when this user
  // has no email recorded.
  const emailCell = _usersShowEmails
    ? `<span class="users-row-email">${u.email ? escHtml(u.email) : ''}</span>`
    : '';
  // Phase D.2 — last_seen + turns_7d from the activity aggregator.
  // Renders as a compact relative time ("12m", "2h", "3d") with the
  // turn count as a small superscript-ish chip when > 0. Empty "—"
  // for users with no recent activity. Hover for the full timestamp +
  // Phase D.3 deep-dive sparkline.
  let lastSeenCell;
  if (u.last_seen) {
    const rel = _usersRelativeTime(u.last_seen);
    const turnsBadge = (u.turns_7d && u.turns_7d > 0)
      ? `<span style="font-size:0.62rem;color:var(--text2);margin-left:4px" title="${u.turns_7d} turn(s) in the last 7 days">·${u.turns_7d}</span>`
      : '';
    // Phase D.3 — richer hover tooltip showing the user's 30-day
    // activity at a glance: turn count, est. cost, session count,
    // and a sparkline of daily turn counts (oldest → newest).
    // The data comes from aggregate()'s extended return shape.
    const tipLines = [`Last seen: ${u.last_seen}`];
    if (typeof u.turns_30d === 'number' && u.turns_30d > 0) {
      const costStr = (typeof u.cost_30d === 'number')
        ? ` · est. cost $${u.cost_30d.toFixed(2)}`
        : '';
      const sessStr = (typeof u.sessions_30d === 'number')
        ? ` · ${u.sessions_30d} session${u.sessions_30d === 1 ? '' : 's'}`
        : '';
      tipLines.push(`30d: ${u.turns_30d} turn${u.turns_30d === 1 ? '' : 's'}${costStr}${sessStr}`);
      // ASCII sparkline of daily_buckets. buckets[0] = today, so
      // reverse for left-to-right oldest→newest reading. Empty when
      // buckets is missing (pre-D.3 server).
      const sparkline = _usersBuildSparkline(u.daily_buckets);
      if (sparkline) tipLines.push(`30d trend: ${sparkline}`);
    }
    const tip = tipLines.join('\n');
    lastSeenCell = `<span class="users-row-lastseen" title="${escHtml(tip)}">${escHtml(rel)}${turnsBadge}</span>`;
  } else {
    lastSeenCell = `<span class="users-row-lastseen" style="color:var(--text3)" title="No turns recorded in the last 30 days">—</span>`;
  }
  // Role control — segmented toggle when editable; locked label for
  // pod-admin (admin role comes from network.json pod-admin claim, not
  // editable here). The button itself is both selector and indicator:
  // active = currently this role; click to switch.
  const role = u.role || 'participant';
  let roleControl;
  if (role === 'admin') {
    roleControl = `<span class="users-role-locked" title="Pod-admin role is set in the Pod Admins card above — not editable per-bot.">Pod-Admin</span>`;
  } else if (role === 'blocked') {
    // Defensive — blocked users normally don't appear in approved[],
    // but if state drifts (block index set, allowFrom not yet revoked),
    // surface the locked label rather than the segmented toggle.
    roleControl = `<span class="users-role-locked" style="color:#f85149" title="Blocked — use Unblock to remove from block index.">Blocked</span>`;
  } else {
    roleControl = `
      <span class="users-role-toggle" title="Click to switch role">
        <button class="${role === 'primary_user' ? 'active' : ''}"
                onclick="_usersPatchRole('${escHtml(botId)}','${escHtml(channel)}','${escHtml(u.id)}','primary_user')">Primary</button>
        <button class="${role === 'participant' ? 'active' : ''}"
                onclick="_usersPatchRole('${escHtml(botId)}','${escHtml(channel)}','${escHtml(u.id)}','participant')">Participant</button>
      </span>`;
  }
  return `
    <div class="users-row">
      <span class="users-row-name">${name}${handle}${labels}</span>
      <code class="users-row-id">${escHtml(u.id)}</code>
      <span class="users-row-surfaces" title="Engagement surfaces — where the bot honors this user">${surfacesText}</span>
      ${lastSeenCell}
      ${emailCell}
      ${roleControl}
      <span class="users-row-actions">
        <button class="btn btn-sm btn-danger" onclick="_usersBlockUser('${escHtml(botId)}','${escHtml(channel)}','${escHtml(u.id)}')" title="Sticky block — survives re-pairing. Use Disconnect for normal removal.">Block</button>
        <button class="btn btn-sm btn-warning" onclick="_usersRevokeUser('${escHtml(botId)}','${escHtml(channel)}','${escHtml(u.id)}')" title="Remove from allowlist. User can re-pair via /start.">Disconnect</button>
      </span>
    </div>
  `;
}

// R1a — row for the "Channel access (group)" list. These identities are
// authorized in openclaw.json's group allowlist (channels.<ch>.allowFrom
// under groupPolicy:allowlist) — a SEPARATE OpenClaw gate from the DM pairing
// store the Approved list manages, and the two routinely diverge. PR2 adds a
// Revoke action (writes ONLY the group allowlist, never the DM store); the
// `config` badge keeps the provenance visible. Layout mirrors
// _usersRenderApprovedRow so the columns align in the same channel block.
function _usersRenderGroupRow(botId, channel, u) {
  const name = u.display_name
    ? escHtml(u.display_name)
    : '<span style="color:var(--text2)">[unknown]</span>';
  const handle = (u.username && u.username !== u.display_name)
    ? `<span style="color:var(--text2);font-size:0.78rem;margin-left:5px">@${escHtml(u.username)}</span>`
    : '';
  const labels = (u.labels || []).map(l =>
    `<span class="badge badge-muted" style="font-size:0.62rem;margin-left:4px" title="${escHtml(l)} (from network.json identity)">${escHtml(l)}</span>`
  ).join('');
  const emailCell = _usersShowEmails
    ? `<span class="users-row-email">${u.email ? escHtml(u.email) : ''}</span>`
    : '';
  let lastSeenCell;
  if (u.last_seen) {
    const rel = _usersRelativeTime(u.last_seen);
    const turnsBadge = (u.turns_7d && u.turns_7d > 0)
      ? `<span style="font-size:0.62rem;color:var(--text2);margin-left:4px" title="${u.turns_7d} turn(s) in the last 7 days">·${u.turns_7d}</span>`
      : '';
    lastSeenCell = `<span class="users-row-lastseen" title="Last seen: ${escHtml(u.last_seen)}">${escHtml(rel)}${turnsBadge}</span>`;
  } else {
    lastSeenCell = `<span class="users-row-lastseen" style="color:var(--text3)" title="No turns recorded in the last 30 days">—</span>`;
  }
  // Role is informational on this list (resolved via the same canonical
  // join), shown as a locked label — the group allowlist is config-managed
  // so per-row role edits don't belong here.
  const roleLabel = escHtml(u.role || 'participant');
  return `
    <div class="users-row">
      <span class="users-row-name">${name}${handle}${labels}</span>
      <code class="users-row-id">${escHtml(u.id)}</code>
      <span class="users-row-surfaces" title="Authorized in group/channel contexts via the bot's openclaw.json group allowlist">GROUP</span>
      ${lastSeenCell}
      ${emailCell}
      <span class="users-role-locked" style="font-style:italic" title="Role resolved from the roster. Group access is the config allowlist; the segmented role toggle lives on the DM-approved list.">${roleLabel}</span>
      <span class="users-row-actions">
        <span class="badge badge-neutral" style="font-size:0.62rem" title="Source: openclaw.json channels.${escHtml(channel)} group allowlist (config). Managed separately from DM approval.">config</span>
        <button class="btn btn-sm btn-warning" onclick="_usersGroupRevoke('${escHtml(botId)}','${escHtml(channel)}','${escHtml(u.id)}')" title="Remove from the channel/group allowlist in openclaw.json. Does NOT change DM access. Restarts the bot gateway to apply.">Revoke</button>
      </span>
    </div>
  `;
}

// R1a PR2 — add-by-id box at the foot of the "Channel access · group"
// section. Lets the operator authorize a new identity in the config group
// allowlist. The id is the channel-native id (Slack U…/W…, Telegram numeric,
// etc.) — the same shape the rows above display. Width follows data shape
// (a short id → input-w-md), per the style guide.
function _usersRenderGroupAddRow(botId, channel) {
  const inputId = `group-add-${escHtml(botId)}-${escHtml(channel)}`;
  return `
    <div style="display:flex;align-items:center;gap:6px;margin-top:6px">
      <input id="${inputId}" type="text" class="input-w-md" placeholder="Add id to group allowlist"
             title="Channel-native id (Slack U…/W…, Telegram numeric). Authorizes this identity in the bot's channel/group allowlist — separate from DM approval."
             onkeydown="if(event.key==='Enter'){_usersGroupApprove('${escHtml(botId)}','${escHtml(channel)}')}">
      <button class="btn btn-sm btn-green" onclick="_usersGroupApprove('${escHtml(botId)}','${escHtml(channel)}')"
              title="Add this id to the channel/group allowlist in openclaw.json. Does NOT grant DM access. Restarts the bot gateway to apply.">Approve</button>
    </div>
  `;
}

function _usersRenderBlockedSection(botId, blocked) {
  if (!blocked || !blocked.length) return '';
  const rows = blocked.map(b => {
    const when = b.blocked_at ? _usersRelativeTime(b.blocked_at) : '';
    const reason = b.reason ? `<span style="font-size:0.78rem;color:var(--text2);margin-left:8px">${escHtml(b.reason)}</span>` : '';
    return `
      <div style="display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--border);font-size:0.85rem;flex-wrap:wrap">
        <span style="min-width:90px;text-transform:capitalize">${escHtml(b.platform)}</span>
        <code style="font-size:0.78rem;background:var(--bg);padding:2px 6px;border-radius:3px;color:var(--text2)">${escHtml(b.id)}</code>
        ${reason}
        ${when ? `<span style="font-size:0.74rem;color:var(--text2)">${escHtml(when)}</span>` : ''}
        <span style="flex:1"></span>
        <button class="btn btn-sm" onclick="_usersUnblockUser('${escHtml(botId)}','${escHtml(b.platform)}','${escHtml(b.id)}')" style="padding:2px 8px;font-size:0.72rem" title="Removes from block index. Does NOT re-admit — they must re-pair explicitly.">Unblock</button>
      </div>
    `;
  }).join('');
  return `
    <div style="margin-bottom:14px;padding:10px 12px;border:1px solid var(--border);border-radius:6px;background:var(--bg2)">
      <div style="font-size:0.85rem;font-weight:600;margin-bottom:6px">Blocked</div>
      <div style="font-size:0.74rem;color:var(--text2);margin-bottom:6px">Sticky deny — survives re-pairing. Unblock does not auto-admit.</div>
      <div>${rows}</div>
    </div>
  `;
}


// ════════════════════════════════════════════════════════════════════════
// Directory — Person cards (spec-user-directory-2026-06-22 §6, Phase 2)
//
// The full per-bot directory: admitted Users and address-book Contacts in one
// filterable list, each a Person card with editable emails (add / edit /
// set-primary / verify / delete) + contact attributes, provenance badges, and
// — for a Contact — an Admit action that runs the EXISTING admission flow
// (the /users/approve route), never a membership write through the directory
// routes. Purely additive to the page; the per-channel sections above are
// untouched.
// ════════════════════════════════════════════════════════════════════════

async function _usersFetchAndRenderDirectory(botId) {
  const target = document.getElementById(`users-directory-${botId}`);
  if (!target) return;
  try {
    const data = await api('GET', `/api/admin/bots/${encodeURIComponent(botId)}/directory`);
    if (!data || data.error) {
      const msg = (data && data.error) ? data.error : 'no directory in response';
      target.innerHTML = `<div style="color:var(--orange);font-size:0.78rem">Failed to load directory: ${escHtml(msg)}</div>`;
      return;
    }
    _usersDirDataByBot[botId] = data;
    _usersRenderDirectory(target, botId, data);
  } catch (e) {
    target.innerHTML = `<div style="color:var(--orange);font-size:0.78rem">Failed to load directory: ${escHtml(String(e))}</div>`;
  }
}

function _usersSetDirFilter(botId, filter) {
  _usersDirFilter = filter;
  const data = _usersDirDataByBot[botId];
  const target = document.getElementById(`users-directory-${botId}`);
  if (data && target) _usersRenderDirectory(target, botId, data);
}

function _usersRenderDirectory(el, botId, data) {
  const persons = data.persons || [];
  if (!persons.length) {
    el.innerHTML = `<div class="empty" style="font-size:0.78rem;padding:8px 0">No people in the directory yet. Emails and contacts the bot records — or that you add here — will appear in this list.</div>`;
    return;
  }
  const userCount = persons.filter(p => !!p.membership).length;
  const contactCount = persons.length - userCount;
  const f = _usersDirFilter;
  const filtered = persons.filter(p =>
    f === 'users' ? !!p.membership : f === 'contacts' ? !p.membership : true);
  // Filter segmented toggle (Users / Contacts / All). Rendered inside the list
  // so a filter change re-renders it with the correct active state — no
  // separate DOM bookkeeping.
  const tab = (key, label) =>
    `<button class="${f === key ? 'active' : ''}" onclick="_usersSetDirFilter('${_dirArg(botId)}','${key}')">${label}</button>`;
  const toggle = `
    <div class="users-mode-toggle" style="margin:0 0 10px 0" title="Filter the directory by membership">
      ${tab('all', `All (${persons.length})`)}
      ${tab('users', `Users (${userCount})`)}
      ${tab('contacts', `Contacts (${contactCount})`)}
    </div>`;
  const body = filtered.length
    ? filtered.map(p => _usersRenderPersonCard(botId, p)).join('')
    : `<div class="empty" style="font-size:0.78rem;padding:8px 0">No ${escHtml(f)} in the directory.</div>`;
  el.innerHTML = toggle + body;
}

function _usersRenderPersonCard(botId, p) {
  const ids = p.identities || [];
  // identities[0] is the base/key identity the directory store is keyed on (see
  // resolver._merge_identities) — the write target for email/contact edits.
  const base = ids[0] || {};
  const platform = base.platform || '';
  const stableId = base.id || '';
  const display = (p.names && p.names.display)
    ? escHtml(p.names.display)
    : '<span style="color:var(--text2)">[unnamed]</span>';
  const membershipBadge = p.membership
    ? `<span class="badge badge-blue" style="font-size:0.66rem" title="Admitted user of this bot — role ${escHtml(p.membership.role)}">${escHtml(p.membership.role)}</span>`
    : `<span class="badge badge-gray" style="font-size:0.66rem" title="Contact — in the directory but not an admitted user. Admit to grant membership.">contact</span>`;
  const idChips = ids.map(i => {
    const who = i.handle ? `@${escHtml(i.handle)}` : escHtml(i.id);
    return `<span class="badge badge-neutral" style="font-size:0.62rem" title="${escHtml(i.platform)} · ${escHtml(i.id)} · source: ${escHtml(i.source || 'unknown')}">${escHtml(i.platform)} ${who}</span>`;
  }).join(' ');
  const primary = (p.emails || []).find(e => e.rank === 'primary');
  const summaryEmail = primary
    ? `<span style="color:var(--text2);font-size:0.74rem">${escHtml(primary.addr)}</span>`
    : '';
  const admitRow = p.membership ? '' : _usersRenderAdmitRow(botId, p);
  return `
    <details class="dir-person" style="border:1px solid var(--border);border-radius:6px;background:var(--bg2);margin-bottom:8px;padding:7px 10px">
      <summary style="display:flex;align-items:center;gap:8px">
        <span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
        <span style="font-weight:600;font-size:0.85rem">${display}</span>
        ${membershipBadge}
        <span style="flex:1;display:flex;gap:4px;flex-wrap:wrap;align-items:center">${idChips}</span>
        ${summaryEmail}
      </summary>
      <div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border)">
        ${admitRow}
        ${_usersRenderEmailsEditor(botId, platform, stableId, p.emails || [])}
        ${_usersRenderContactEditor(botId, platform, stableId, p.contact || {})}
      </div>
    </details>`;
}

// Provenance badge — semantic token colors (no new hex). operator-verified
// (green, authoritative) > bot-asserted (yellow, "from the bot, unverified —
// confirm it") > channel-captured (neutral, the platform reported it).
function _usersProvenanceBadge(prov) {
  if (prov === 'operator-verified') {
    return `<span class="badge badge-green" style="font-size:0.62rem" title="Operator-verified — you entered or confirmed this in the admin UI. Outranks bot-asserted.">operator</span>`;
  }
  if (prov === 'bot-asserted') {
    return `<span class="badge badge-yellow" style="font-size:0.62rem" title="From the bot, UNVERIFIED — the bot recorded this via its directory tool. Confirm it's correct, then edit/save to mark it operator-verified.">bot · unverified</span>`;
  }
  return `<span class="badge badge-neutral" style="font-size:0.62rem" title="Channel-captured — the messaging platform reported this (e.g. a Slack profile email).">channel</span>`;
}

function _usersRenderEmailsEditor(botId, platform, stableId, emails) {
  // Primary first, then secondaries in their stored order.
  const sorted = emails.slice().sort((a, b) =>
    (a.rank === 'primary' ? 0 : 1) - (b.rank === 'primary' ? 0 : 1));
  const rows = sorted.length
    ? sorted.map((e, i) => _usersRenderEmailRow(botId, platform, stableId, e, i)).join('')
    : `<div style="font-size:0.72rem;color:var(--text3);padding:2px 0">No emails recorded.</div>`;
  const slug = _dirSlug(`${platform}:${stableId}`);
  return `
    <div style="margin-bottom:10px">
      <div style="font-size:0.72rem;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:4px">Emails</div>
      ${rows}
      <div style="display:flex;align-items:center;gap:6px;margin-top:5px;flex-wrap:wrap">
        <input id="dir-addem-${slug}" type="email" class="input-w-lg" placeholder="add email address"
               onkeydown="if(event.key==='Enter'){_usersDirAddEmail('${_dirArg(botId)}','${_dirArg(platform)}','${_dirArg(stableId)}')}">
        <select id="dir-addemrank-${slug}" class="input-w-sm" title="Rank for the new email">
          <option value="secondary">secondary</option>
          <option value="primary">primary</option>
        </select>
        <button class="btn btn-sm btn-green" style="padding:1px 8px;font-size:0.7rem"
                onclick="_usersDirAddEmail('${_dirArg(botId)}','${_dirArg(platform)}','${_dirArg(stableId)}')">Add</button>
      </div>
    </div>`;
}

function _usersRenderEmailRow(botId, platform, stableId, e, idx) {
  const slug = _dirSlug(`${platform}:${stableId}`);
  const inputId = `dir-em-${slug}-${idx}`;
  const isPrimary = e.rank === 'primary';
  const a = (op, extra) =>
    `_usersDirEmailOp('${_dirArg(botId)}','${_dirArg(platform)}','${_dirArg(stableId)}','${op}','${_dirArg(e.addr)}'${extra || ''})`;
  const rankControl = isPrimary
    ? `<span class="badge badge-primary" style="font-size:0.62rem" title="Primary contact email">primary</span>`
    : `<button class="btn btn-sm" style="padding:1px 7px;font-size:0.66rem" onclick="${a('set_primary')}" title="Make this the primary email — demotes the current primary in one atomic flip">Make primary</button>`;
  return `
    <div class="dir-email-row" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding:3px 0">
      <input id="${inputId}" type="email" class="input-w-lg" value="${escHtml(e.addr)}" title="Edit the address, then Save">
      ${rankControl}
      ${_usersProvenanceBadge(e.provenance)}
      <label style="font-size:0.7rem;color:var(--text2);display:inline-flex;align-items:center;gap:3px;cursor:pointer" title="Mark this address confirmed-real">
        <input type="checkbox" ${e.verified ? 'checked' : ''} style="cursor:pointer"
               onchange="${a('set_verified', ',this.checked')}"> verified
      </label>
      <button class="btn btn-sm" style="padding:1px 7px;font-size:0.66rem"
              onclick="_usersDirSaveEmail('${_dirArg(botId)}','${_dirArg(platform)}','${_dirArg(stableId)}','${_dirArg(e.addr)}','${inputId}')"
              title="Save the edited address">Save</button>
      <button class="btn btn-sm btn-warning" style="padding:1px 7px;font-size:0.66rem"
              onclick="${a('delete')}" title="Remove this email">Delete</button>
    </div>`;
}

function _usersRenderContactEditor(botId, platform, stableId, contact) {
  const slug = _dirSlug(`${platform}:${stableId}`);
  const cid = `dir-contact-${slug}`;
  const keys = Object.keys(contact || {});
  const rows = keys.map(k => _usersRenderContactRow(k, contact[k])).join('');
  return `
    <div>
      <div style="font-size:0.72rem;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:4px">Contact attributes</div>
      <div id="${cid}">
        ${rows || `<div class="dir-contact-empty" style="font-size:0.72rem;color:var(--text3);padding:2px 0">No contact attributes.</div>`}
      </div>
      <div style="display:flex;align-items:center;gap:6px;margin-top:5px">
        <button class="btn btn-sm" style="padding:1px 8px;font-size:0.7rem"
                onclick="_usersDirAddContactRow('${cid}')" title="Add a new attribute (e.g. phone, org, notes)">+ attribute</button>
        <button class="btn btn-sm btn-green" style="padding:1px 8px;font-size:0.7rem"
                onclick="_usersDirSaveContact('${_dirArg(botId)}','${_dirArg(platform)}','${_dirArg(stableId)}','${cid}')"
                title="Save all contact attributes (stamped operator-verified)">Save contact</button>
      </div>
    </div>`;
}

function _usersRenderContactRow(k, v) {
  return `
    <div class="dir-contact-row" style="display:flex;align-items:center;gap:6px;padding:2px 0">
      <input type="text" class="dir-ck input-w-md" value="${escHtml(k || '')}" placeholder="attribute" title="Attribute name (e.g. phone, org, notes)">
      <span style="color:var(--text3)">:</span>
      <input type="text" class="dir-cv input-w-lg" value="${escHtml(v == null ? '' : String(v))}" placeholder="value">
      <button class="btn btn-sm btn-warning" style="padding:1px 7px;font-size:0.66rem"
              onclick="this.closest('.dir-contact-row').remove()" title="Remove this attribute (Save contact to persist)">×</button>
    </div>`;
}

function _usersRenderAdmitRow(botId, p) {
  const ids = p.identities || [];
  const ch = ids.find(i => _USERS_KNOWN_CHANNELS.includes(i.platform));
  if (!ch) {
    return `<div style="font-size:0.72rem;color:var(--text3);margin-bottom:8px">Address-book contact — no messaging identity, so there's nothing to admit on. Pair them on a channel first.</div>`;
  }
  return `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;padding:6px 8px;border:1px solid var(--border);border-radius:5px;background:var(--bg)">
      <span style="font-size:0.72rem;color:var(--text2);flex:1">Contact — not an admitted user. Admit on ${escHtml(ch.platform)} to grant participant membership via the standard admission flow.</span>
      <button class="btn btn-sm btn-green" style="padding:2px 9px;font-size:0.7rem"
              onclick="_usersDirAdmit('${_dirArg(botId)}','${_dirArg(ch.platform)}','${_dirArg(ch.id)}')"
              title="Runs the existing admission flow (adds to allowFrom) — the SAME path as approving a paired user. Does not use the directory write routes.">Admit</button>
    </div>`;
}

// ── Directory write actions ───────────────────────────────────────────────

async function _usersDirPost(botId, url, body, label) {
  try {
    const resp = await api('POST', url, body);
    if (resp && resp.error) { toast(`${label} failed: ${resp.error}`, 'err'); return false; }
    _usersFetchAndRenderDirectory(botId);
    return true;
  } catch (e) {
    toast(`${label} failed: ${e}`, 'err');
    return false;
  }
}

async function _usersDirAddEmail(botId, platform, stableId) {
  const slug = _dirSlug(`${platform}:${stableId}`);
  const inp = document.getElementById(`dir-addem-${slug}`);
  const rankSel = document.getElementById(`dir-addemrank-${slug}`);
  const addr = inp ? inp.value.trim() : '';
  if (!addr) { toast('Enter an email address to add.', 'err'); return; }
  const rank = rankSel ? rankSel.value : 'secondary';
  await _usersDirPost(botId, _dirUrl(botId, platform, stableId, 'email'),
    { op: 'add', addr, rank }, 'Add email');
}

async function _usersDirEmailOp(botId, platform, stableId, op, addr, verified) {
  if (op === 'delete' && !await confirmModal({body: `Remove ${addr} from this person?`, danger: true})) return;
  const body = { op, addr };
  if (op === 'set_verified') body.verified = !!verified;
  await _usersDirPost(botId, _dirUrl(botId, platform, stableId, 'email'), body, 'Email update');
}

async function _usersDirSaveEmail(botId, platform, stableId, addr, inputId) {
  const inp = document.getElementById(inputId);
  const newAddr = inp ? inp.value.trim() : '';
  if (!newAddr) { toast('Email address cannot be empty.', 'err'); return; }
  if (newAddr === addr) return;  // unchanged — nothing to save
  await _usersDirPost(botId, _dirUrl(botId, platform, stableId, 'email'),
    { op: 'update', addr, new_addr: newAddr }, 'Save email');
}

function _usersDirAddContactRow(cid) {
  const c = document.getElementById(cid);
  if (!c) return;
  const empty = c.querySelector('.dir-contact-empty');
  if (empty) empty.remove();
  c.insertAdjacentHTML('beforeend', _usersRenderContactRow('', ''));
}

async function _usersDirSaveContact(botId, platform, stableId, cid) {
  const c = document.getElementById(cid);
  if (!c) return;
  const contact = {};
  c.querySelectorAll('.dir-contact-row').forEach(row => {
    const kEl = row.querySelector('.dir-ck');
    const vEl = row.querySelector('.dir-cv');
    const key = kEl ? kEl.value.trim() : '';
    const val = vEl ? vEl.value.trim() : '';
    if (key) contact[key] = val;  // blank values clear the attribute server-side
  });
  await _usersDirPost(botId, _dirUrl(botId, platform, stableId, 'contact'),
    { contact }, 'Save contact');
}

// Admit a Contact → User. Runs the EXISTING admission flow (the same
// /users/approve route the per-channel lists use), NOT a directory write —
// membership is never mutated through the directory routes (invariant #2).
async function _usersDirAdmit(botId, platform, id) {
  if (!await confirmModal(`Admit ${id} as a participant on ${botLabel(botId)}'s ${platform}?\n\n` +
               `This runs the standard admission flow (adds them to allowFrom) — the ` +
               `same path as approving a paired user. It grants the default participant ` +
               `role and does NOT change the directory's emails/contact data.`)) {
    return;
  }
  try {
    const resp = await api('POST',
      `/api/admin/bots/${encodeURIComponent(botId)}/users/approve`,
      { channel: platform, id });
    if (resp && resp.error) { toast(`Admit failed: ${resp.error}`, 'err'); return; }
    _usersFetchAndRenderDirectory(botId);   // contact → user; refresh the directory
    _usersFetchAndRenderByChannel(botId);   // and the per-channel lists
  } catch (e) {
    toast(`Admit failed: ${e}`, 'err');
  }
}

// Page-level toggle for the email column. Re-renders the per-bot panel's
// Users-by-channel section from cached data (no fresh fetch needed —
// the email is already in the GET response, the toggle just controls
// whether _usersRenderApprovedRow surfaces it). Persists to
// localStorage so the operator's choice survives page reloads.
function _usersToggleEmails() {
  _usersShowEmails = !_usersShowEmails;
  try {
    localStorage.setItem(
      'evolve_users_show_emails', _usersShowEmails ? '1' : '0');
  } catch (_) { /* private window / quota — ignore */ }
  const data = _usersChannelDataByBot[_usersActiveBot];
  if (!data) return;
  const target = document.getElementById(`users-by-channel-${_usersActiveBot}`);
  if (target) _usersRenderByChannel(target, _usersActiveBot, data);
}

function _usersRenderPendingRow(botId, channel, req) {
  const meta = req.meta || {};
  const fullName = [meta.firstName, meta.lastName].filter(Boolean).join(' ') || meta.name || meta.username || '[unnamed]';
  const username = meta.username ? `<span style="color:var(--text2);margin-left:6px;font-size:0.78rem">@${escHtml(meta.username)}</span>` : '';
  const created = req.createdAt ? _usersRelativeTime(req.createdAt) : '';
  if (req.auto_approve_eligible) {
    return `
      <div style="display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--border);font-size:0.85rem">
        <span style="min-width:140px">${escHtml(fullName)}${username}</span>
        <code style="font-size:0.78rem;background:var(--bg);padding:2px 6px;border-radius:3px;color:var(--text2)">${escHtml(req.id)}</code>
        <span style="font-size:0.78rem;color:#3fb950">⟳ Auto-approving (${escHtml(req.auto_approve_reason || 'known pod admin')})…</span>
      </div>
    `;
  }
  return `
    <div style="padding:6px 0;border-bottom:1px solid var(--border);font-size:0.85rem">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span style="min-width:140px">${escHtml(fullName)}${username}</span>
        <code style="font-size:0.78rem;background:var(--bg);padding:2px 6px;border-radius:3px;color:var(--text2)">${escHtml(req.id)}</code>
        <code style="font-size:0.78rem;background:var(--bg);padding:2px 6px;border-radius:3px;color:var(--text2)">code ${escHtml(req.code || '')}</code>
        <span style="flex:1"></span>
        <button class="btn btn-primary btn-sm" onclick="_usersApproveRequest('${escHtml(botId)}','${escHtml(channel)}','${escHtml(req.id)}','${escHtml(req.code || '')}')" style="padding:2px 10px;font-size:0.74rem">Approve</button>
        <button class="btn btn-sm btn-warning" onclick="_usersRejectRequest('${escHtml(botId)}','${escHtml(channel)}','${escHtml(req.id)}','${escHtml(req.code || '')}')" style="padding:2px 10px;font-size:0.74rem">Reject</button>
      </div>
      ${created ? `<div style="font-size:0.74rem;color:var(--text2);margin-top:2px">Created ${escHtml(created)}</div>` : ''}
    </div>
  `;
}

function _channelDisplayName(channel) {
  const m = { telegram: 'Telegram', slack: 'Slack', discord: 'Discord', whatsapp: 'WhatsApp', signal: 'Signal' };
  return m[channel] || channel;
}

// ═══════════════════════════════════════════════════════════════════════════
//  Per-user tier defaults (G5, spec-user-tier-control 2026-08-03 addendum)
// ═══════════════════════════════════════════════════════════════════════════
//
// Read-only surface over {sharedDir}/{botId}/user-tier-prefs.json — the
// standing per-user model-tier defaults the `evo tier-default` chat command
// writes. The pod admin previously had NO visibility into these; this section
// shows, per user, the standing default that routing applies above the
// bot-wide Conversations default. Writes stay on the chat surface (and G4's
// future bot-invocable tool) — no edit control here by design.

// Role-id → display label. Mirrors the canonical labels in
// ai-optimization.js's TIER_DISPLAY (kept local: SPA modules avoid top-level
// cross-file coupling and this section only needs the four user-settable
// roles). Unknown values fall back to the raw string so nothing is hidden.
const _USERS_TIER_LABELS = { fast: 'Fast', standard: 'Standard', power: 'Power', max: 'Max' };

async function _usersFetchAndRenderTierPrefs(botId) {
  const el = document.getElementById(`users-tier-prefs-${botId}`);
  if (!el) return;
  try {
    const data = await api('GET', `/api/admin/bots/${encodeURIComponent(botId)}/users/tier-prefs`);
    if (!data || data.error || !Array.isArray(data.users)) {
      const msg = (data && data.error) ? data.error : 'unexpected response';
      el.innerHTML = `<div style="color:var(--orange);font-size:0.8rem">Failed to load tier defaults: ${escHtml(msg)}</div>`;
      return;
    }
    _usersRenderTierPrefs(el, botId, data.users);
  } catch (e) {
    el.innerHTML = `<div style="color:var(--orange);font-size:0.8rem">Failed to load tier defaults: ${escHtml(String(e))}</div>`;
  }
}

// Best-effort join from a pref row's (channel, external_id) to a display name
// in the already-fetched by_channel identity lists. Returns null when no
// friendly identity matches — the caller then shows the raw user_key, never
// hides the entry.
function _usersTierPrefDisplayName(botId, pref) {
  if (!pref.channel || !pref.external_id) return null;
  const chData = ((_usersChannelDataByBot[botId] || {}).by_channel || {})[pref.channel];
  if (!chData) return null;
  const lists = [chData.approved, chData.group_access, chData.pending, chData.seen_recently];
  for (const list of lists) {
    for (const u of (list || [])) {
      if (String(u.id) === String(pref.external_id) && u.display_name) {
        return u.display_name;
      }
    }
  }
  return null;
}

function _usersRenderTierPrefs(el, botId, users) {
  if (!users.length) {
    el.innerHTML = '<div style="font-size:0.82rem;color:var(--text2);padding:4px 0">'
      + 'No per-user tier defaults set — users can say '
      + '<code>evo tier-default power</code> to a bot to set one.</div>';
    return;
  }
  const rows = users.map(pref => {
    const label = _USERS_TIER_LABELS[pref.default_role] || pref.default_role;
    const name = _usersTierPrefDisplayName(botId, pref);
    // Identity cell: friendly name (with the raw key on hover) when the join
    // hit; otherwise the raw user_key so unjoinable entries stay visible.
    const who = name
      ? `<span title="${escHtml(pref.user_key)}">${escHtml(name)}</span>`
      : `<code style="font-size:0.78rem" title="No matching identity on this bot's channel lists — showing the raw per-user key">${escHtml(pref.user_key)}</code>`;
    const chBadge = pref.channel
      ? `<span class="badge badge-neutral" style="font-size:0.62rem">${escHtml(_channelDisplayName(pref.channel))}</span>`
      : '';
    const updated = pref.updated_at
      ? `<span style="font-size:0.74rem;color:var(--text3)" title="${escHtml(pref.updated_at)}">${escHtml(_usersRelativeTime(pref.updated_at))}</span>`
      : '';
    return `
      <div style="display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--border);font-size:0.85rem">
        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${who}</span>
        ${chBadge}
        <span class="badge badge-neutral" title="Standing default tier for this user's conversations (set via 'evo tier-default')">${escHtml(label)}</span>
        ${updated}
      </div>
    `;
  }).join('');
  el.innerHTML = rows
    + '<div style="font-size:0.72rem;color:var(--text3);margin-top:5px">'
    + 'Set from the chat thread with <code>evo tier-default fast|standard|power|max</code>; '
    + '<code>evo tier-default auto</code> clears it. Applied above the bot-wide Conversations default.</div>';
}

function _usersRelativeTime(isoStr) {
  try {
    const t = new Date(isoStr).getTime();
    const dt = Date.now() - t;
    if (dt < 60000) return `${Math.max(1, Math.round(dt / 1000))} sec ago`;
    if (dt < 3600000) return `${Math.round(dt / 60000)} min ago`;
    if (dt < 86400000) return `${Math.round(dt / 3600000)} hr ago`;
    return `${Math.round(dt / 86400000)} days ago`;
  } catch (_) { return ''; }
}

// Phase D.3 — render a Unicode block-element sparkline from a daily-bucket
// array. buckets[0] = today, [1] = yesterday … so we reverse for the
// left-to-right oldest-to-newest reading operators expect. Returns ''
// when buckets is missing or all-zero (no signal to show).
//
// Glyphs span U+2581 ▁ (1/8) through U+2588 █ (8/8). All-zero buckets
// render as low dots so the sparkline still draws a baseline ("trend
// line is flat" reads better than "trend is missing").
function _usersBuildSparkline(buckets) {
  if (!Array.isArray(buckets) || !buckets.length) return '';
  const max = Math.max(...buckets);
  if (max === 0) return '';  // no activity at all in the window
  const glyphs = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];
  // Reverse so oldest reads left, newest reads right — matches the
  // reading order operators apply to time-series.
  return buckets.slice().reverse().map(n => {
    if (n === 0) return '·';   // distinguishes zero days from low-count days
    const ratio = n / max;
    const idx = Math.min(glyphs.length - 1, Math.max(0,
      Math.floor(ratio * glyphs.length)));
    return glyphs[idx];
  }).join('');
}

function _usersRefreshPendingChips() {
  if (window.__usersIdentityData) _usersRenderBotTiles(window.__usersIdentityData);
  let total = 0;
  Object.values(_usersPendingCounts).forEach(n => { total += n; });
  const chip = document.getElementById('users-pending-chip');
  if (chip) {
    if (total > 0) {
      chip.textContent = `● ${total} pending`;
      chip.style.display = '';
    } else {
      chip.style.display = 'none';
    }
  }
  const badge = document.getElementById('badge-users-pending');
  if (badge) {
    if (total > 0) {
      badge.textContent = String(total);
      badge.style.display = '';
    } else {
      badge.style.display = 'none';
    }
  }
}

async function _usersApproveRequest(botId, channel, id, code) {
  try {
    const resp = await api('POST', `/api/admin/bots/${encodeURIComponent(botId)}/users/approve`, { channel, id, code });
    if (resp && resp.error) { toast(`Approve failed: ${resp.error}`, 'err'); return; }
    _usersFetchAndRenderByChannel(botId);
  } catch (e) {
    toast(`Approve failed: ${e}`, 'err');
  }
}

// Admit an identity surfaced from turn history (the "Active · not
// admitted" lane, not /start pairing). Reuses the existing approve
// endpoint; the _approve dispatcher tolerates an ID that's not in
// pairing.json (it's a no-op filter then), so this directly adds to
// allowFrom. (Function name kept as ...ApproveSeen — it maps to the
// /approve route; only the operator-facing label is "Admit".)
async function _usersApproveSeen(botId, channel, id, displayName) {
  const label = displayName ? `${displayName} (${id})` : id;
  if (!await confirmModal(`Admit ${label} as a participant on ${botLabel(botId)}'s ${channel}?\n\n` +
               `They messaged this bot recently but never went through /start. ` +
               `Admitting adds them to allowFrom; they get the default participant role.`)) {
    return;
  }
  try {
    const resp = await api('POST',
      `/api/admin/bots/${encodeURIComponent(botId)}/users/approve`,
      { channel, id });
    if (resp && resp.error) {
      toast(`Approve failed: ${resp.error}`, 'err');
      return;
    }
    _usersFetchAndRenderByChannel(botId);
  } catch (e) {
    toast(`Approve failed: ${e}`, 'err');
  }
}

// Triage-dismiss an "Active · not admitted" identity. Unlike Block this
// has no admission side effects — it only writes the overlay ignore
// index so the row stops reappearing on every page load. The user keeps
// whatever access they already had.
async function _usersIgnoreSeen(botId, channel, id) {
  if (!await confirmModal(`Ignore ${id} on ${botLabel(botId)}'s ${channel}?\n\n` +
               `Removes this row from the "Active · not admitted" list. ` +
               `It does NOT block them or change their access — it just ` +
               `dismisses the row so you don't have to triage it again.`)) {
    return;
  }
  try {
    const resp = await api('POST',
      `/api/admin/bots/${encodeURIComponent(botId)}/users/${encodeURIComponent(channel)}/${encodeURIComponent(id)}/ignore`);
    if (resp && resp.error) {
      toast(`Ignore failed: ${resp.error}`, 'err');
      return;
    }
    _usersFetchAndRenderByChannel(botId);
  } catch (e) {
    toast(`Ignore failed: ${e}`, 'err');
  }
}

async function _usersRejectRequest(botId, channel, id, code) {
  if (!await confirmModal({body: `Reject pairing request from ${id}? Their pairing code will be invalidated.`, danger: true})) return;
  try {
    const resp = await api('POST', `/api/admin/bots/${encodeURIComponent(botId)}/users/reject`, { channel, id, code });
    if (resp && resp.error) { toast(`Reject failed: ${resp.error}`, 'err'); return; }
    _usersFetchAndRenderByChannel(botId);
  } catch (e) {
    toast(`Reject failed: ${e}`, 'err');
  }
}

async function _usersRevokeUser(botId, channel, id) {
  if (!await confirmModal({body: `Disconnect ${id} from ${botLabel(botId)} on ${channel}? They will lose access until they re-pair via /start.`, danger: true})) return;
  try {
    const resp = await api('POST', `/api/admin/bots/${encodeURIComponent(botId)}/users/revoke`, { channel, id });
    if (resp && resp.error) { toast(`Disconnect failed: ${resp.error}`, 'err'); return; }
    _usersFetchAndRenderByChannel(botId);
  } catch (e) {
    toast(`Disconnect failed: ${e}`, 'err');
  }
}

// ── R1a PR2: group/channel allowlist management ──────────────────────────
//
// Distinct from DM approve/revoke above. These write ONLY
// openclaw.json::channels.<ch> group allowlist (a separate OpenClaw gate);
// the backend validates against the OC schema, writes 0600, and restarts the
// gateway so the change takes effect. The confirm copy spells out that this is
// channel/group access, NOT DM access, so the operator can't conflate them.
async function _usersGroupApprove(botId, channel) {
  const input = document.getElementById(`group-add-${botId}-${channel}`);
  const id = input ? input.value.trim() : '';
  if (!id) { toast('Enter an id to add to the group allowlist.', 'err'); return; }
  if (!await confirmModal(`Authorize ${id} in ${botLabel(botId)}'s ${channel} group/channel allowlist?\n\n` +
               `This grants channel/group access only — it does NOT grant DM access. ` +
               `The bot gateway restarts to apply the change.`)) {
    return;
  }
  try {
    const resp = await api('POST',
      `/api/admin/bots/${encodeURIComponent(botId)}/users/group-allowlist/approve`,
      { channel, id });
    if (resp && resp.error) { toast(`Group approve failed: ${resp.error}`, 'err'); return; }
    if (resp && resp.gateway_restart_warning) { toast(resp.gateway_restart_warning, 'ok'); }
    _usersFetchAndRenderByChannel(botId);
  } catch (e) {
    toast(`Group approve failed: ${e}`, 'err');
  }
}

async function _usersGroupRevoke(botId, channel, id) {
  if (!await confirmModal({body: `Revoke ${id} from ${botLabel(botId)}'s ${channel} group/channel allowlist?\n\n` +
               `They lose channel/group access. This does NOT change their DM access ` +
               `(managed separately under "Approved · DM"). The bot gateway restarts to apply.`, danger: true})) {
    return;
  }
  try {
    const resp = await api('POST',
      `/api/admin/bots/${encodeURIComponent(botId)}/users/group-allowlist/revoke`,
      { channel, id });
    if (resp && resp.error) { toast(`Group revoke failed: ${resp.error}`, 'err'); return; }
    if (resp && resp.gateway_restart_warning) { toast(resp.gateway_restart_warning, 'ok'); }
    _usersFetchAndRenderByChannel(botId);
  } catch (e) {
    toast(`Group revoke failed: ${e}`, 'err');
  }
}

// ── Overlay mutations (spec 2026-06-07) ──────────────────────────────────

async function _usersPatchRole(botId, channel, id, role) {
  // Spec 2026-06-07 §3: explicit overlay role overrides primary_owner
  // default. Blocking is a separate action (Block button); the role
  // select only offers primary_user / participant.
  try {
    const resp = await api(
      'PATCH',
      `/api/admin/bots/${encodeURIComponent(botId)}/users/${encodeURIComponent(channel)}/${encodeURIComponent(id)}`,
      { role }
    );
    if (resp && resp.error) {
      toast(`Set role failed: ${resp.error}`, 'err');
      _usersFetchAndRenderByChannel(botId);  // re-sync UI to server state
      return;
    }
    _usersFetchAndRenderByChannel(botId);
  } catch (e) {
    toast(`Set role failed: ${e}`, 'err');
  }
}

async function _usersBlockUser(botId, channel, id) {
  // Block is more severe than Disconnect — it (a) revokes from
  // allowFrom, (b) writes a sticky block-index entry that survives
  // re-pairing, (c) rejects any pending pairing requests. The confirm
  // copy spells that out so an operator doesn't reach for Block when
  // Disconnect would do.
  const reason = prompt(
    `Block ${id} from ${botId} on ${channel}?\n\n` +
    `• They lose access immediately.\n` +
    `• Any pending pairing code is rejected.\n` +
    `• They cannot re-pair via /start (unlike Disconnect).\n` +
    `• Unblocking them later does NOT auto-re-admit — you'd have to ` +
    `approve a fresh pairing.\n\n` +
    `Optional reason for the audit log:`
  );
  if (reason === null) return;  // cancel
  try {
    const resp = await api(
      'POST',
      `/api/admin/bots/${encodeURIComponent(botId)}/users/${encodeURIComponent(channel)}/${encodeURIComponent(id)}/block`,
      { reason }
    );
    if (resp && resp.error) {
      // Partial-success surface (backend returns 500 with
      // partial:true when overlay block recorded but allowFrom revoke
      // failed) — re-fetch so the UI shows whatever state did persist.
      toast(`Block failed: ${resp.error}`, 'err');
      _usersFetchAndRenderByChannel(botId);
      return;
    }
    _usersFetchAndRenderByChannel(botId);
  } catch (e) {
    toast(`Block failed: ${e}`, 'err');
  }
}

async function _usersUnblockUser(botId, channel, id) {
  if (!await confirmModal(
    `Unblock ${id} on ${channel}?\n\n` +
    `This removes them from the block index but does NOT re-admit them. ` +
    `They would need to send /start again and be approved.`
  )) return;
  try {
    const resp = await api(
      'POST',
      `/api/admin/bots/${encodeURIComponent(botId)}/users/${encodeURIComponent(channel)}/${encodeURIComponent(id)}/unblock`
    );
    if (resp && resp.error) {
      toast(`Unblock failed: ${resp.error}`, 'err');
      _usersFetchAndRenderByChannel(botId);
      return;
    }
    _usersFetchAndRenderByChannel(botId);
  } catch (e) {
    toast(`Unblock failed: ${e}`, 'err');
  }
}

async function _usersSetNewcomerMode(botId, channel, mode) {
  // No confirm — this is a config change, not a destructive action.
  // The select-box change itself is the operator's commit.
  try {
    const resp = await api(
      'PUT',
      `/api/admin/bots/${encodeURIComponent(botId)}/channels/${encodeURIComponent(channel)}/newcomer_mode`,
      { mode }
    );
    if (resp && resp.error) {
      toast(`Set newcomer mode failed: ${resp.error}`, 'err');
      _usersFetchAndRenderByChannel(botId);
      return;
    }
    _usersFetchAndRenderByChannel(botId);
  } catch (e) {
    toast(`Set newcomer mode failed: ${e}`, 'err');
  }
}

// Segmented-toggle entry point. Clicked from the per-bot panel header
// (Single-user | Multi-user). Each segment passes its TARGET value
// (false for Single-user, true for Multi-user) plus the CURRENT value
// so the wrapper can no-op when the operator clicks the already-active
// segment instead of firing a spurious confirm dialog.
function _usersSetMultiUser(botId, target, currentlyMulti) {
  if (target === currentlyMulti) return;
  return _usersToggleMultiUser(botId, currentlyMulti);
}

// Pill toggle on the per-bot Users panel. Server side is the existing
// PATCH /api/bot/multi-user — single source of truth in network.json.
// Direction-aware confirm copy spells out the consequences each way so
// the operator doesn't flip a live bot by accident.
async function _usersToggleMultiUser(botId, currentlyMulti) {
  const next = !currentlyMulti;
  const label = next ? 'multi-user' : 'single-user';
  const msg = next
    ? `Switch ${botId} to multi-user?\n\n` +
      `• Per-user signal partitioning starts (profiles, alerts, and ` +
      `improvement proposals scope per user_key).\n` +
      `• The bot's existing primary user (if any) becomes the primary ` +
      `among potentially many users.\n` +
      `• Telegram/Slack policy gates that previously trusted any DM now ` +
      `require pod-admin or owner status.\n` +
      `• Multi-user posture checks turn on (admin claimed, primary ` +
      `recorded, exec scoped).`
    : `Switch ${botId} to single-user?\n\n` +
      `• Alert routing collapses to the primary user.\n` +
      `• Per-user signal partitioning stops.\n` +
      `• Other paired users in OC's allowFrom still have access at the ` +
      `OC layer — they're just no longer special-cased by Evolve.\n` +
      `• Multi-user posture checks turn off.`;
  if (!await confirmModal(msg)) return;
  const r = await api('PATCH', '/api/bot/multi-user', { botId, multiUser: next });
  if (r?.ok) {
    toast(`✓ ${botLabel(botId)} is now ${label}`, 'ok');
    // Refresh identity (drives bot tiles + per-bot panel) and the
    // Overview status (so the dashboard chip also flips).
    try { await loadIdentity(); } catch (_) {}
    try { await loadStatus(); } catch (_) {}
  } else {
    toast(`✗ ${r?.error || 'Failed to switch mode'}`, 'err');
  }
}

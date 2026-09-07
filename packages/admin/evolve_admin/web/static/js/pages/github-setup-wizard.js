// ════════════════════════════════════════════════════════════════════════
// GitHub setup mini-wizard
//
// Sequential four-section flow launched from the Setup checklist's GitHub
// row. Reuses these endpoints:
//
//   GET  /api/admin/onboard/github/discover-default-pat
//   POST /api/admin/onboard/github/verify
//   POST /api/admin/onboard/github
//   POST /api/skills/install/github/install-mcp-server
//   GET  /api/skills/install/github/status?bot_id=<bot>
//   GET  /api/network
//
// State (_ghSetupState) lives in a module-scoped object so per-section
// render functions can write back into it. Modal close fires
// _setupChecklistRender refresh callbacks so the parent surfaces (chip,
// Settings card, checklist modal) update without a full page reload.
//
// Cluster contents:
//   openGithubSetupWizard(botId) + closeGithubSetupWizard()
//   _ghSetupRender() — driver
//   _ghSetupSection[1-4]Html(s) — per-section markup builders
//   _ghSetupSetStatus(msg, color)
//   _ghSetupUseDiscoveredPat()
//   _ghSetupVerifyPat() + _ghSetupVerifyPatInternal(tokenOrNonce)
//   _ghSetupWireBackup() + _ghSetupWireMcp()
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - api(), toast(), escHtml() — core/
//   - _setupChecklistRender / renderBotSetupChecklistCard — pages/bot-detail.js
//     (Phase 3ac; refresh callback fires on modal close)
//   - loadStatus() (Overview) — refreshed after success
//   - nav() / window.nav — core/router.js
// ════════════════════════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════
// GitHub setup mini-wizard
// ══════════════════════════════════════════════════════
// Sequential four-section flow launched from the Setup checklist's GitHub
// row. Reuses three existing endpoints:
//
//   GET  /api/admin/onboard/github/discover-default-pat
//   POST /api/admin/onboard/github/verify
//   POST /api/admin/onboard/github
//   POST /api/skills/install/github/install-mcp-server
//   GET  /api/skills/install/github/status?bot_id=<bot>
//   GET  /api/network
//
// State lives in a module-scoped object so per-section render functions
// can write back into it (PAT verified → save the token + login →
// section 2 unlocks). Modal close fires _setupChecklistRender refresh
// callbacks so the parent surfaces (chip, Settings card, checklist
// modal) update without a full page reload.

let _ghSetupState = null;

async function openGithubSetupWizard(botId) {
  const overlay = document.getElementById('github-setup-modal');
  const title = document.getElementById('github-setup-modal-title');
  const body = document.getElementById('github-setup-modal-body');
  if (!overlay || !body) return;
  const label = (typeof botLabel === 'function') ? botLabel(botId) : botId;
  if (title) title.textContent = `Connect GitHub backup — ${label}`;
  body.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  overlay.classList.add('open');
  // Module state — section 2's repo input + section 3's wiring need
  // the PAT from section 1, so we stash it here on verify.
  _ghSetupState = {
    botId,
    botLabel: label,
    pat: null,            // raw PAT after verify
    patNonce: null,       // returned by discover-default-pat — token-bound for replay
    login: null,          // github login confirmed by verify
    backupWired: false,   // true once /api/admin/onboard/github returns ok
    backupRepoUrl: null,  // populated on initial load or after wire-backup
    backupRecentPush: false,
    mcpWired: false,      // true once /api/skills/install/github/install-mcp-server succeeds
  };
  // Initial fetch: discover existing PAT, current network.json (for
  // backupRepoUrl), and the github MCP install status. All three are
  // independent — parallelize.
  let discover, networkData, mcpStatus;
  try {
    [discover, networkData, mcpStatus] = await Promise.all([
      api('GET', '/api/admin/onboard/github/discover-default-pat'),
      api('GET', '/api/network'),
      api('GET', `/api/skills/install/github/status?bot_id=${encodeURIComponent(botId)}`),
    ]);
  } catch (e) {
    body.innerHTML = `<div class="subtle" style="color:var(--red)">Failed to load: ${escHtml(String(e && e.message || e))}</div>`;
    return;
  }
  // Seed module state from initial fetches.
  if (discover && discover.has_pat) {
    _ghSetupState.patNonce = discover.nonce || null;
    _ghSetupState.login = discover.login || null;
  }
  const botCfg = (networkData && networkData.bots && networkData.bots[botId]) || {};
  _ghSetupState.backupRepoUrl = botCfg.backupRepoUrl || null;
  _ghSetupState.backupWired = !!_ghSetupState.backupRepoUrl;
  _ghSetupState.mcpWired = !!(mcpStatus && mcpStatus.ok && mcpStatus.status === 'valid');
  _ghSetupRender();
}

function closeGithubSetupWizard() {
  const overlay = document.getElementById('github-setup-modal');
  if (overlay) overlay.classList.remove('open');
  const botId = _ghSetupState && _ghSetupState.botId;
  _ghSetupState = null;
  // Refresh the surfaces the operator came from — tile chip counter,
  // Settings card, parent checklist modal — so the github row reflects
  // anything they did inside the wizard.
  try {
    if (typeof refreshTiles === 'function') refreshTiles();
    else if (typeof _refreshOverview === 'function') _refreshOverview();
  } catch (_) { /* not on Overview */ }
  try {
    const settingsEl = document.getElementById('botcfg-setup-checklist');
    if (settingsEl && typeof _configBot !== 'undefined' && _configBot) {
      renderBotSetupChecklistCard(_configBot);
    }
  } catch (_) { /* not on Settings page */ }
  try {
    // If the checklist modal is open, re-fetch its state too so the
    // github row's badge updates from pending → done.
    const cmOverlay = document.getElementById('setup-checklist-modal');
    if (cmOverlay && cmOverlay.classList.contains('open') && botId) {
      openSetupChecklistModal(botId);
    }
  } catch (_) { /* best-effort */ }
}

function _ghSetupRender() {
  const body = document.getElementById('github-setup-modal-body');
  if (!body || !_ghSetupState) return;
  const s = _ghSetupState;
  body.innerHTML = `
    ${_ghSetupSection2Html(s)}
    ${_ghSetupSection1Html(s)}
    ${_ghSetupSection3Html(s)}
    ${_ghSetupSection4Html(s)}
    <div id="github-setup-status" class="subtle"
         style="margin-top:10px;font-size:0.78rem;min-height:1.2em"></div>
  `;
}

function _ghSetupSection1Html(s) {
  const haveCred = !!(s.pat || s.patNonce);
  const verifiedLabel = s.login
    ? `<span style="color:var(--green)">✓ Verified as ${escHtml(s.login)}</span>`
    : '';
  const discoveredHint = (!s.pat && s.patNonce && s.login)
    ? `<div class="subtle" style="font-size:0.76rem;margin-bottom:6px">Found existing PAT for <code>${escHtml(s.login)}</code>. Use it or paste a different one below.</div>`
    : '';
  const useDiscoveredBtn = (!s.pat && s.patNonce)
    ? `<button class="btn btn-sm" onclick="_ghSetupUseDiscoveredPat()" style="margin-right:6px">Use discovered PAT</button>`
    : '';
  // PAT description defaults to the repo-name convention so the token shows
  // up legibly on github.com/settings/tokens. We use the convention default
  // (botId-workspace) rather than reading the live repo input — the input
  // value isn't preserved across re-renders, and the user can rename the
  // token on github.com if they pick a custom repo name.
  const patDescription = `${s.botId}-workspace`;
  return `
    <div class="card" style="margin-bottom:12px;padding:12px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
        <div style="font-weight:600">2. Credentials</div>
        ${verifiedLabel}
      </div>
      ${discoveredHint}
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
        ${useDiscoveredBtn}
        <input id="github-setup-pat-input" type="password" placeholder="ghp_… or github_pat_…"
               style="flex:1;min-width:220px;padding:6px 8px;background:var(--bg2);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:0.85rem">
        <button class="btn btn-primary btn-sm" onclick="_ghSetupVerifyPat()">Verify</button>
      </div>
      <div class="subtle" style="font-size:0.72rem;margin-top:6px">
        Needs <code>repo</code> scope. Generate at
        <a href="https://github.com/settings/tokens/new?scopes=repo&description=${encodeURIComponent(patDescription)}"
           target="_blank" rel="noopener">github.com/settings/tokens/new</a>.
      </div>
    </div>
  `;
}

function _ghSetupSection2Html(s) {
  const defaultRepoName = `${s.botId}-workspace`;
  const existingHint = s.backupRepoUrl
    ? `<div class="subtle" style="font-size:0.76rem;margin-bottom:6px">Currently wired to <code>${escHtml(s.backupRepoUrl)}</code>. Type a different name to rewire, or just confirm below.</div>`
    : '';
  return `
    <div class="card" style="margin-bottom:12px;padding:12px">
      <div style="font-weight:600;margin-bottom:6px">1. Repository</div>
      ${existingHint}
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
        <input id="github-setup-repo-input" type="text" placeholder="${escHtml(defaultRepoName)}"
               value="${escHtml(defaultRepoName)}"
               style="flex:1;min-width:220px;padding:6px 8px;background:var(--bg2);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:0.85rem">
        <label style="font-size:0.74rem;display:flex;align-items:center;gap:4px">
          <input id="github-setup-reuse-checkbox" type="checkbox">
          Reuse if exists
        </label>
      </div>
      <div class="subtle" style="font-size:0.72rem;margin-top:6px">
        Private repo under <code>${escHtml(s.login || '<your account>')}</code>. The bot's nightly backup pushes here. Check "Reuse if exists" if you're pointing at a repo you already created.
      </div>
    </div>
  `;
}

function _ghSetupSection3Html(s) {
  const ready = !!(s.login && (s.pat || s.patNonce));
  const dimStyle = ready ? '' : 'opacity:0.45;pointer-events:none';
  const wiredLabel = s.backupWired
    ? `<span style="color:var(--green);font-size:0.78rem">✓ Wired${s.backupRepoUrl ? ' to ' + escHtml(s.backupRepoUrl.split('/').slice(-2).join('/')) : ''}</span>`
    : '';
  const btnLabel = s.backupWired ? 'Re-wire backup' : 'Wire backup';
  return `
    <div class="card" style="margin-bottom:12px;padding:12px;${dimStyle}">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
        <div style="font-weight:600">3. Wire backup</div>
        ${wiredLabel}
      </div>
      <div class="subtle" style="font-size:0.76rem;margin-bottom:6px">
        Creates the repo (if it doesn't exist), registers this bot's SSH deploy key, and sets <code>network.json::bots.${escHtml(s.botId)}.backupRepoUrl</code>.
      </div>
      <button class="btn btn-primary btn-sm" onclick="_ghSetupWireBackup()">${btnLabel}</button>
    </div>
  `;
}

function _ghSetupSection4Html(s) {
  const ready = !!(s.pat || s.patNonce);
  const dimStyle = ready ? '' : 'opacity:0.45;pointer-events:none';
  const wiredLabel = s.mcpWired
    ? `<span style="color:var(--green);font-size:0.78rem">✓ GitHub MCP installed</span>`
    : '';
  return `
    <div class="card" style="margin-bottom:12px;padding:12px;${dimStyle}">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
        <div style="font-weight:600">4. Enable GitHub tools (MCP)</div>
        ${wiredLabel}
      </div>
      <div class="subtle" style="font-size:0.76rem;margin-bottom:6px">
        Adds the GitHub MCP server to this bot's openclaw.json so the bot can read repos, browse code, and file issues through GitHub's API. Same PAT, separate from the backup wiring.
      </div>
      <button class="btn btn-primary btn-sm" onclick="_ghSetupWireMcp()">${s.mcpWired ? 'Re-enable' : 'Enable for this bot'}</button>
    </div>
  `;
}

function _ghSetupSetStatus(msg, color) {
  const el = document.getElementById('github-setup-status');
  if (!el) return;
  el.textContent = msg;
  el.style.color = color || 'var(--text2)';
}

function _ghSetupUseDiscoveredPat() {
  // The nonce IS the credential reference — server-side _resolve_credential
  // accepts either a raw PAT or a nonce. Mark it verified and unlock the
  // rest of the flow.
  if (!_ghSetupState || !_ghSetupState.patNonce) return;
  // Verify the nonce-bound PAT so we get the login for display.
  _ghSetupVerifyPatInternal(_ghSetupState.patNonce);
}

async function _ghSetupVerifyPat() {
  const input = document.getElementById('github-setup-pat-input');
  const pat = (input && input.value || '').trim();
  if (!pat) {
    _ghSetupSetStatus('Paste a PAT first.', 'var(--red)');
    return;
  }
  _ghSetupVerifyPatInternal(pat);
}

async function _ghSetupVerifyPatInternal(tokenOrNonce) {
  if (!_ghSetupState) return;
  _ghSetupSetStatus('Verifying…');
  let r;
  try {
    // Server shape (see web/server.py::api_admin_onboard_verify_github):
    //   request:  { default: {token, github_login}, bots: [{bot_id, repo_name, ...}] }
    //   response: { ok, bots: [{bot_id, ok, login, has_repo_scope, ...}], available_orgs }
    // Previous bug: we sent `token` at the top level (server reads default.token
    // and saw none → per-bot "no token" error) and read `r.results[0].login`
    // (server uses `bots`, not `results`). Both wrong → fell through to the
    // misleading "token may lack repo scope" message even with a valid PAT.
    r = await api(
      'POST',
      '/api/admin/onboard/github/verify',
      {
        default: { token: tokenOrNonce, github_login: '' },
        bots: [{ bot_id: _ghSetupState.botId, repo_name: 'verify-only' }],
      },
    );
  } catch (e) {
    _ghSetupSetStatus(`Verify failed: ${String(e && e.message || e)}`, 'var(--red)');
    return;
  }
  if (!r || r.error) {
    _ghSetupSetStatus(`Verify failed: ${escHtml((r && r.error) || 'unknown')}`, 'var(--red)');
    return;
  }
  // Per-bot results are in r.bots[]. Surface the per-bot error verbatim
  // when verify failed so the operator sees the real cause (bad token,
  // insufficient scope, GitHub /user 401/403, etc.) instead of a generic
  // "no login" message.
  const botResult = (r.bots && r.bots[0]) || null;
  if (!botResult || !botResult.ok) {
    const err = (botResult && botResult.error) || 'unknown error';
    _ghSetupSetStatus(`Verify failed: ${escHtml(err)}`, 'var(--red)');
    return;
  }
  const login = botResult.login || botResult.actual_login || null;
  if (!login) {
    _ghSetupSetStatus('Verify returned no login — token may lack repo scope.', 'var(--red)');
    return;
  }
  _ghSetupState.login = login;
  // If the verify input was a raw PAT, stash it for downstream calls.
  // If it was a nonce, the server will redeem it again on subsequent
  // requests (the nonce is reusable within its TTL).
  if (tokenOrNonce.startsWith('ghp_') || tokenOrNonce.startsWith('github_pat_') || tokenOrNonce.startsWith('ghs_')) {
    _ghSetupState.pat = tokenOrNonce;
  } else {
    _ghSetupState.patNonce = tokenOrNonce;
  }
  _ghSetupSetStatus(`Verified as ${login}.`, 'var(--green)');
  _ghSetupRender();
}

async function _ghSetupWireBackup() {
  if (!_ghSetupState || !_ghSetupState.login) return;
  const repoInput = document.getElementById('github-setup-repo-input');
  const reuseCb = document.getElementById('github-setup-reuse-checkbox');
  const repo = (repoInput && repoInput.value || '').trim();
  if (!repo) {
    _ghSetupSetStatus('Type a repo name first.', 'var(--red)');
    return;
  }
  _ghSetupSetStatus('Wiring backup… (this can take a few seconds)');
  const tokenRef = _ghSetupState.pat || _ghSetupState.patNonce;
  let r;
  try {
    r = await api(
      'POST',
      '/api/admin/onboard/github',
      {
        default: { token: tokenRef, github_login: _ghSetupState.login },
        bots: [{
          bot_id: _ghSetupState.botId,
          repo_name: repo,
          reuse_confirmed: !!(reuseCb && reuseCb.checked),
        }],
      },
    );
  } catch (e) {
    _ghSetupSetStatus(`Wire failed: ${String(e && e.message || e)}`, 'var(--red)');
    return;
  }
  if (!r || r.error) {
    _ghSetupSetStatus(`Wire failed: ${escHtml((r && r.error) || 'unknown')}`, 'var(--red)');
    return;
  }
  const result = (r.results || [])[0] || {};
  if (!result.ok) {
    _ghSetupSetStatus(`Wire failed: ${escHtml(result.error || 'unknown')}`, 'var(--red)');
    return;
  }
  _ghSetupState.backupWired = true;
  _ghSetupState.backupRepoUrl = result.backupRepoUrl || result.repo_url
    || `https://github.com/${_ghSetupState.login}/${repo}`;
  _ghSetupSetStatus('Backup wired. The nightly daemon will push on its next run; check Backup status for the first success.', 'var(--green)');
  _ghSetupRender();
}

async function _ghSetupWireMcp() {
  if (!_ghSetupState) return;
  const tokenRef = _ghSetupState.pat || _ghSetupState.patNonce;
  if (!tokenRef) {
    _ghSetupSetStatus('Verify a PAT first (section 1).', 'var(--red)');
    return;
  }
  _ghSetupSetStatus('Enabling GitHub MCP…');
  let r;
  try {
    r = await api(
      'POST',
      '/api/skills/install/github/install-mcp-server',
      { bot_id: _ghSetupState.botId, access_token: tokenRef },
    );
  } catch (e) {
    _ghSetupSetStatus(`MCP enable failed: ${String(e && e.message || e)}`, 'var(--red)');
    return;
  }
  if (!r || !r.ok) {
    _ghSetupSetStatus(`MCP enable failed: ${escHtml((r && r.error) || 'unknown')}`, 'var(--red)');
    return;
  }
  _ghSetupState.mcpWired = true;
  _ghSetupSetStatus('GitHub MCP installed. The bot can now use GitHub tools (repos, issues, code search).', 'var(--green)');
  _ghSetupRender();
}

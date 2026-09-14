// ════════════════════════════════════════════════════════════════════════
// Page subtab: Guided onboarding modal (github + brave)
//
// Single-screen modal driven by a small state object. The credential is
// either pasted by the user OR auto-discovered via the discover endpoint
// (which returns a session nonce — the plaintext token never reaches the
// browser). Verify populates per-bot collision data; Set Up runs the
// fanout endpoint and renders inline progress.
//
// Entry points (callers via runtime free-variable lookup):
//   - Plugins → Credentials add-key flow → "Onboard for all bots" CTA
//   - Maintenance → Backup card → "Guided setup" CTA on unconfigured bot
//   - Recovery rollback empty state → "Set up GitHub backup" link
//   - openGithubBackupRotate (this file) — token rotation flow
//
// State:
//   _onboardState — provider, bots, preselectedOwner, discoveredNonce,
//   discoveredLogin, discoveredSourceBot, verifiedLogin, verifiedFineGrained,
//   perBot[bot_id] = {repo_name, github_login?, override?, collision?,
//                      reuse_confirmed?, ...}
//
// Cluster contents:
//   openOnboardModal(provider, preselectedBots, preselectedOwner) — entry
//   closeOnboardModal() — cleanup
//   openGithubBackupRotate() — token rotation re-uses the same modal
//   _onboardCollectBotsMissing(provider) — discover candidate bots
//   _onboardDiscoverDefaultPat() — discover existing PAT on filesystem
//   onboardUseDiscovered / onboardClearDiscovered — discovered PAT toggles
//   _onboardRenderBotsList() — per-bot row renderer with collision banner
//   onboardConfirmReuseFromBanner / onboardScrollToRow — banner actions
//   onboardSetSelected / onboardSetRepoName / onboardSetGithubLogin /
//   onboardSetReuse / onboardToggleOverride / onboardSetOverrideToken /
//   onboardSetOverrideLogin — per-row state setters
//   _onboardSelectedBots / _onboardUpdateSubmitState
//   onboardVerify() — verify the credential, populate collision data
//   _onboardRenderResult(r, provider) — render the Set Up fanout result
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - api(), toast(), escHtml(), botLabel() — core/
//   - loadStatus() (Overview) — refreshed after onboard completes
//   - ikRenderKeys — pages/credentials.js (Phase 3ad; refreshed after
//     onboard success)
//   - nav() / window.nav — core/router.js
// ════════════════════════════════════════════════════════════════════════

// ── Guided onboarding (github + brave) ──────────────────────────────────
// Single-screen modal driven by a small state object. The credential is
// either pasted by the user OR auto-discovered via the discover endpoint
// (which returns a session nonce — the plaintext token never reaches the
// browser). Verify populates per-bot collision data; Set Up runs the
// fanout endpoint and renders inline progress.

let _onboardState = {
  provider: null,            // 'github' | 'brave'
  bots: [],                  // [bot_id, ...] candidate bots from caller
  preselectedOwner: null,    // when an override-chip click opens for a specific account
  discoveredNonce: null,     // server-side handle for the auto-discovered PAT
  discoveredLogin: null,
  discoveredSourceBot: null,
  // Per-bot org selection (feature 2026-05-04-002):
  // availableOrgs: [{login, type: 'user'|'org'|'unknown', source}], populated
  //   from the discover-default-pat response (PAT's /user + /user/orgs +
  //   discovered bot owners). Re-fetched after verify so a user-pasted PAT's
  //   orgs replace the cascade-discovered set.
  // botOwners: {bot_id: {owner, repo, auth_type}}, the current owner from
  //   .git/config — both HTTPS-PAT and SSH bots show up here.
  availableOrgs: [],
  botOwners: {},
  verifiedLogin: null,       // result of /verify call
  verifiedFineGrained: false,
  perBot: {},                // bot_id → {repo_name, github_login?, override?, collision?, reuse_confirmed, ...}
};

async function openOnboardModal(provider, preselectedBots, preselectedOwner) {
  _onboardState = {
    provider,
    bots: Array.isArray(preselectedBots) && preselectedBots.length ? preselectedBots : [],
    preselectedOwner: preselectedOwner || null,
    discoveredNonce: null,
    discoveredLogin: null,
    discoveredSourceBot: null,
    availableOrgs: [],
    botOwners: {},
    verifiedLogin: null,
    verifiedFineGrained: false,
    perBot: {},
  };
  // If no bots passed, default to "all bots that need this integration"
  // computed from the keys API across the pod (fan-out scan).
  if (!_onboardState.bots.length) {
    _onboardState.bots = await _onboardCollectBotsMissing(provider);
  }
  // Initialize per-bot rows. github_login starts empty — discover-default-pat
  // populates it from .git/config in _onboardDiscoverDefaultPat below, and
  // pre-selecting an override owner via the chip path forces it.
  for (const b of _onboardState.bots) {
    _onboardState.perBot[b] = _onboardState.perBot[b] || {
      repo_name: provider === 'github' ? `${b}-workspace` : null,
      github_login: preselectedOwner || '',
      override_token: '',
      override_login: '',
      override_open: false,
      reuse_confirmed: false,
      collision: null,
      verify: null,
    };
  }

  document.getElementById('onboard-modal-title').textContent =
    provider === 'github' ? 'Set up GitHub backup' : 'Set up Brave search';
  document.getElementById('onboard-modal-help').innerHTML = provider === 'github'
    ? `Need a token? <a href="https://github.com/settings/tokens/new?scopes=repo&description=evolve-backup" target="_blank" rel="noopener">Create a classic PAT with <code>repo</code> scope ▶</a> (one click). One token covers every bot whose repo it can read — we'll use it to create the private backup repo, register a deploy key, push backups, and verify each repo stays private. A <a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener">fine-grained PAT</a> works too — pick <em>All repositories</em>, then permissions <em>Contents: read+write</em>, <em>Administration: read+write</em>, <em>Metadata: read</em>.`
    : `Get a free key at <a href="https://api.search.brave.com/app/keys" target="_blank" rel="noopener">api.search.brave.com/app/keys ▶</a>, then paste it below.`;
  document.getElementById('onboard-cred-label').textContent =
    provider === 'github' ? 'Personal Access Token' : 'Brave API Key';
  document.getElementById('onboard-cred-input').value = '';
  document.getElementById('onboard-cred-input').setAttribute('placeholder',
    provider === 'github' ? 'ghp_… or github_pat_…' : 'BSA…');
  document.getElementById('onboard-cred-status').innerHTML = '';
  document.getElementById('onboard-result').innerHTML = '';
  document.getElementById('onboard-discover-row').style.display = 'none';
  document.getElementById('onboard-submit-btn').disabled = true;
  document.getElementById('onboard-submit-btn').style.display = '';
  document.getElementById('onboard-submit-btn').textContent = '▶ Set Up';
  document.getElementById('onboard-cancel-btn').textContent = 'Cancel';

  _onboardRenderBotsList();
  document.getElementById('onboard-modal').classList.add('open');

  // For github, kick off PAT auto-discovery so the operator can [Use] it.
  if (provider === 'github') {
    await _onboardDiscoverDefaultPat();
  }
}

function closeOnboardModal() {
  document.getElementById('onboard-modal').classList.remove('open');
}

// Re-open the GitHub backup wizard in "rotate / re-run" mode, preloaded
// with every bot that already has a backup repo configured. Lets the
// operator paste a fresh PAT to (a) re-verify each repo, (b) rewrite each
// bot's .git/config with the new token, (c) persist the new token to
// the keystore (key github_pat) so the backup-visibility monitor can confirm
// privacy. The onboard endpoint is idempotent — existing repos with our
// deploy key already attached re-use silently.
//
// When no bots are configured yet, falls through to the regular wizard
// which auto-collects bots with missing keys (first-time setup path).
function openGithubBackupRotate() {
  const bots = (typeof _networkData !== 'undefined' && _networkData && _networkData.bots)
    ? _networkData.bots : {};
  const configured = Object.keys(bots)
    .filter(b => ((bots[b] || {}).backupRepoUrl || '').trim())
    .sort();
  if (!configured.length) {
    openOnboardModal('github');
    return;
  }
  openOnboardModal('github', configured);
}

async function _onboardCollectBotsMissing(provider) {
  // Fan out across all bots: read keys API for each, collect those whose
  // status for this provider is "missing" (NOT "opted_out" — that's intentional).
  const allBots = (typeof _networkData !== 'undefined' && _networkData && _networkData.bots)
    ? Object.keys(_networkData.bots) : [];
  if (!allBots.length) return [];
  const out = [];
  for (const b of allBots) {
    try {
      const d = await api('GET', `/api/admin/keys/${b}`);
      const row = (d && d.keys || []).find(k => k.provider === provider);
      if (row && row.status === 'missing') out.push(b);
    } catch (_) {}
  }
  return out;
}

async function _onboardDiscoverDefaultPat() {
  try {
    const r = await api('GET', '/api/admin/onboard/github/discover-default-pat');
    if (!r) return;
    // Per-bot current owners — populated even when no PAT is discoverable.
    // Used to render the "currently at <owner>/<repo>" line and to seed each
    // bot's github_login dropdown so the existing config is the default.
    _onboardState.botOwners = r.bot_owners || {};
    _onboardState.availableOrgs = Array.isArray(r.available_orgs) ? r.available_orgs : [];
    for (const b of _onboardState.bots) {
      const st = _onboardState.perBot[b];
      if (!st) continue;
      const currentOwner = (_onboardState.botOwners[b] || {}).owner || '';
      // Only seed when nothing is already set (preselectedOwner from chip path
      // takes precedence; later verify can also override via re-render).
      if (!st.github_login) st.github_login = currentOwner || '';
    }
    if (r.nonce) {
      _onboardState.discoveredNonce = r.nonce;
      _onboardState.discoveredLogin = r.login;
      _onboardState.discoveredSourceBot = r.source_bot;
      const row = document.getElementById('onboard-discover-row');
      row.innerHTML = `↳ Discovered <code>${escHtml(r.masked || '')}</code> from <strong>${escHtml(r.source_bot)}</strong>'s .git/config — <a href="javascript:void(0)" onclick="onboardUseDiscovered()">[Use]</a> | <a href="javascript:void(0)" onclick="onboardClearDiscovered()">[Clear]</a>`;
      row.style.display = 'block';
    }
    // Re-render so per-bot rows pick up the new owner + dropdown options.
    _onboardRenderBotsList();
  } catch (_) {}
}

function onboardUseDiscovered() {
  if (!_onboardState.discoveredNonce) return;
  document.getElementById('onboard-cred-input').value = _onboardState.discoveredNonce;
  document.getElementById('onboard-cred-status').innerHTML = `<span style="color:var(--text3)">Discovered nonce loaded — click Verify to confirm.</span>`;
}

function onboardClearDiscovered() {
  document.getElementById('onboard-cred-input').value = '';
  document.getElementById('onboard-discover-row').style.display = 'none';
  _onboardState.discoveredNonce = null;
}

function _onboardRenderBotsList() {
  const el = document.getElementById('onboard-bots-list');
  const provider = _onboardState.provider;
  const bots = _onboardState.bots;
  if (!bots.length) {
    el.innerHTML = '<div style="font-size:0.78rem;color:var(--text3)">No bots need this integration.</div>';
    return;
  }
  const q = v => JSON.stringify(v).replace(/"/g, '&quot;');
  el.innerHTML = bots.map(b => {
    const st = _onboardState.perBot[b] || {};
    const verify = st.verify || null;
    let verifyLine = '';
    if (verify) {
      if (!verify.ok) {
        verifyLine = `<div style="font-size:0.72rem;color:var(--red);margin-top:4px">⚠ ${escHtml(verify.error || 'verify failed')}</div>`;
      } else if (provider === 'github') {
        const repo = verify.repo || {};
        if (!repo.exists) {
          verifyLine = `<div style="font-size:0.72rem;color:var(--green);margin-top:4px">✓ Will create <code>${escHtml(verify.login)}/${escHtml(st.repo_name)}</code></div>`;
        } else if (repo.has_evolve_pubkey) {
          verifyLine = `<div style="font-size:0.72rem;color:var(--green);margin-top:4px">✓ Will reuse <code>${escHtml(verify.login)}/${escHtml(st.repo_name)}</code> (already has evolve deploy key)</div>`;
        } else {
          const last = repo.last_pushed_at ? `<div style="font-size:0.7rem;color:var(--text3)">Last push: ${escHtml(repo.last_pushed_at)}</div>` : '';
          verifyLine = `<div style="font-size:0.72rem;color:var(--orange);margin-top:4px">
            ⚠ <a href="${escHtml(repo.url || '#')}" target="_blank" rel="noopener">${escHtml(verify.login)}/${escHtml(st.repo_name)}</a> already exists. Confirm reuse OR rename.${last}
            <div style="margin-top:4px"><label style="font-weight:normal">
              <input type="checkbox" onchange="onboardSetReuse(${q(b)},this.checked)" ${st.reuse_confirmed ? 'checked' : ''}> Reuse this repo
            </label></div>
          </div>`;
        }
      } else if (provider === 'brave') {
        if (verify.eligible) {
          verifyLine = `<div style="font-size:0.72rem;color:var(--green);margin-top:4px">✓ Will set brave provider</div>`;
        } else if (verify.already_configured) {
          const where = verify.oc_only ? 'openclaw.json (not auth-profiles)' : 'auth-profiles.json';
          verifyLine = `<div style="font-size:0.72rem;color:var(--text3);margin-top:4px">⊘ Already configured in ${where} — uncheck to skip, or check to overwrite</div>`;
        } else {
          verifyLine = `<div style="font-size:0.72rem;color:var(--text3);margin-top:4px">⊘ Opted out (provider: ${escHtml(verify.current_provider || 'unknown')}) — uncheck to skip</div>`;
        }
      }
    }
    let detailRow = '';
    if (provider === 'github') {
      // "Currently at" line + per-bot org selector (feature 2026-05-04-002).
      const owner = (_onboardState.botOwners[b] || {}).owner || '';
      const repo = (_onboardState.botOwners[b] || {}).repo || '';
      const authType = (_onboardState.botOwners[b] || {}).auth_type || '';
      let currentLine = '';
      if (owner) {
        const authBadge = authType === 'ssh'
          ? ' <span style="font-size:0.7rem;color:var(--text3)">(via SSH)</span>'
          : '';
        currentLine = `<div style="margin-left:24px;font-size:0.72rem;color:var(--text3)">↳ currently at <code>${escHtml(owner)}/${escHtml(repo)}</code>${authBadge}</div>`;
      }

      // Org dropdown options: union of available_orgs (PAT can write to) +
      // the bot's current owner if not already in the list, so re-running
      // the wizard for an SSH-only bot whose owner the PAT can't see still
      // pre-selects sanely. Free-form text input is the override-account
      // path, kept as the escape hatch.
      const orgOpts = (_onboardState.availableOrgs || []).slice();
      if (owner && !orgOpts.find(o => o.login === owner)) {
        orgOpts.push({login: owner, type: 'unknown', source: 'discovered_from_bot'});
      }
      const selected = st.github_login || owner || '';
      const orgOptionsHtml = orgOpts.length
        ? orgOpts.map(o => {
            const tag = o.source === 'pat_user' ? ' (you)'
                      : o.source === 'pat_orgs' ? ' (org)'
                      : '';
            const isSel = (o.login === selected) ? ' selected' : '';
            return `<option value="${escHtml(o.login)}"${isSel}>${escHtml(o.login)}${tag}</option>`;
          }).join('')
        : '';
      const orgSelector = orgOptionsHtml
        ? `<select class="form-select input-w-auto" onchange="onboardSetGithubLogin(${q(b)},this.value)"
             style="font-size:0.78rem;padding:3px 6px;font-family:monospace;margin-left:6px">
             ${selected ? '' : '<option value="">— pick org —</option>'}
             ${orgOptionsHtml}
           </select>`
        : `<input type="text" value="${escHtml(selected)}" placeholder="github owner"
             onchange="onboardSetGithubLogin(${q(b)},this.value)"
             style="font-size:0.78rem;padding:3px 6px;background:var(--bg2);border:1px solid var(--border);color:var(--text);border-radius:4px;width:160px;font-family:monospace;margin-left:6px">`;

      detailRow = currentLine + `
        <div style="margin-left:24px;margin-top:4px;display:flex;align-items:center;flex-wrap:wrap;gap:4px">
          <span style="font-size:0.72rem;color:var(--text3)">Repo:</span>
          ${orgSelector}
          <span style="font-size:0.72rem;color:var(--text3)">/</span>
          <input type="text" value="${escHtml(st.repo_name || '')}"
            onchange="onboardSetRepoName(${q(b)},this.value)"
            style="font-size:0.78rem;padding:3px 6px;background:var(--bg2);border:1px solid var(--border);color:var(--text);border-radius:4px;width:200px;font-family:monospace">
          <a href="javascript:void(0)" style="font-size:0.72rem;margin-left:8px"
            onclick="onboardToggleOverride(${q(b)})">${st.override_open ? '[hide override]' : '[▶ Use a different account]'}</a>
        </div>`;
      if (st.override_open) {
        detailRow += `<div style="margin-left:24px;margin-top:4px;padding:6px;background:var(--bg2);border:1px solid var(--border);border-radius:4px">
          <div style="font-size:0.72rem;color:var(--text3);margin-bottom:4px">Override credential for <strong>${escHtml(botLabel(b))}</strong> — only needed when this bot's repo lives under an account the default PAT can't reach</div>
          <input type="password" placeholder="Override PAT"
            onchange="onboardSetOverrideToken(${q(b)},this.value)"
            style="font-size:0.78rem;padding:3px 6px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;width:240px;font-family:monospace;margin-right:6px">
          <input type="text" placeholder="github username"
            onchange="onboardSetOverrideLogin(${q(b)},this.value)"
            style="font-size:0.78rem;padding:3px 6px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;width:160px;font-family:monospace">
        </div>`;
      }
      detailRow += verifyLine;
    } else {
      detailRow = verifyLine;
    }
    // Selection state must persist across re-renders. _onboardState
    // is the source of truth — read `.selected` (defaulting to true on
    // first sight of a bot so existing flows still bulk-enable). Pre-
    // 2026-06-07 this rendered hardcoded `checked` and re-rendered on
    // every change, so the user's uncheck was reverted instantly. The
    // onchange both saves the new state and re-renders so verify-line
    // chrome stays in sync with the selection.
    if (st.selected === undefined) st.selected = true;
    const selectedAttr = st.selected ? 'checked' : '';
    return `<div id="onboard-row-${escHtml(b)}" style="padding:6px 0;border-bottom:1px solid var(--border)">
      <label style="display:inline-flex;align-items:center;gap:6px;font-size:0.85rem">
        <input type="checkbox" data-bot="${escHtml(b)}" ${selectedAttr} onchange="onboardSetSelected(${q(b)},this.checked)">
        <strong>${escHtml(botLabel(b))}</strong>
      </label>
      ${detailRow}
    </div>`;
  }).join('');
}

// Wizard collision banner — actionable affordances for the
// "Unresolved collisions" path. The banner fires when Set Up
// detected an existing repo for a bot whose verify hadn't been
// re-run since the collision was discovered, so the inline [Reuse]
// checkbox in the bot row was bypassed. Two recovery paths:
//
//   - Reuse: flip reuse_confirmed=true for that bot and scroll to
//     its row so the operator sees the checkbox now ticked. No
//     re-verify needed — the collision banner already proved the
//     repo exists; reuse_confirmed is the operator's blessing.
//   - Rename: just scroll to the row so the operator can edit the
//     repo_name input in place. The verify side requires a re-Verify
//     after the rename anyway.
//
// Pre-fix the banner said "set Reuse or rename." as plain text —
// the operator had to scroll back, find the row, and act in a
// completely separate part of the page. This collapses to one click.
function onboardConfirmReuseFromBanner(botId) {
  const st = _onboardState.perBot[botId];
  if (!st) return;
  st.reuse_confirmed = true;
  _onboardRenderBotsList();
  _onboardUpdateSubmitState();
  onboardScrollToRow(botId);
  // Clear the banner once the operator acts — re-running Set Up will
  // re-populate it if another collision still exists.
  const el = document.getElementById('onboard-result');
  if (el) el.innerHTML = `<div class="alert alert-ok">✓ Marked <strong>${escHtml(botLabel(botId))}</strong> for reuse. Click <strong>Set Up</strong> again to retry, or rename the others first.</div>`;
}

function onboardScrollToRow(botId) {
  const row = document.getElementById(`onboard-row-${botId}`);
  if (!row) return;
  row.scrollIntoView({ behavior: 'smooth', block: 'center' });
  // Subtle highlight pulse so the operator's eye lands on the right
  // row after the scroll. Two-step transition stays out of the way of
  // the bot row's own border-bottom styling.
  const prev = row.style.background;
  row.style.transition = 'background 220ms ease-out';
  row.style.background = 'rgba(255,165,2,0.18)';
  setTimeout(() => { row.style.background = prev; }, 1400);
}

function onboardSetSelected(botId, checked) {
  if (!_onboardState.perBot[botId]) return;
  _onboardState.perBot[botId].selected = !!checked;
  _onboardRenderBotsList();
  _onboardUpdateSubmitState();
}

function onboardSetRepoName(botId, value) {
  if (!_onboardState.perBot[botId]) return;
  _onboardState.perBot[botId].repo_name = (value || '').trim();
  _onboardState.perBot[botId].verify = null;  // require re-verify
  document.getElementById('onboard-submit-btn').disabled = true;
}
function onboardSetGithubLogin(botId, value) {
  if (!_onboardState.perBot[botId]) return;
  _onboardState.perBot[botId].github_login = (value || '').trim();
  _onboardState.perBot[botId].verify = null;  // org change requires re-verify
  document.getElementById('onboard-submit-btn').disabled = true;
}
function onboardSetReuse(botId, checked) {
  if (!_onboardState.perBot[botId]) return;
  _onboardState.perBot[botId].reuse_confirmed = !!checked;
  _onboardUpdateSubmitState();
}
function onboardToggleOverride(botId) {
  if (!_onboardState.perBot[botId]) return;
  _onboardState.perBot[botId].override_open = !_onboardState.perBot[botId].override_open;
  _onboardRenderBotsList();
}
function onboardSetOverrideToken(botId, value) {
  if (!_onboardState.perBot[botId]) return;
  _onboardState.perBot[botId].override_token = (value || '').trim();
  _onboardState.perBot[botId].verify = null;
  document.getElementById('onboard-submit-btn').disabled = true;
}
function onboardSetOverrideLogin(botId, value) {
  if (!_onboardState.perBot[botId]) return;
  _onboardState.perBot[botId].override_login = (value || '').trim();
  _onboardState.perBot[botId].verify = null;
  document.getElementById('onboard-submit-btn').disabled = true;
}

function _onboardSelectedBots() {
  const checks = document.querySelectorAll('#onboard-bots-list input[type=checkbox][data-bot]');
  const out = [];
  checks.forEach(c => { if (c.checked) out.push(c.getAttribute('data-bot')); });
  return out;
}

function _onboardUpdateSubmitState() {
  // Submit is enabled when:
  //   - At least one bot is selected
  //   - All selected bots have a verify result with ok=true
  //   - For github: every collision is resolved (reuse_confirmed OR repo doesn't exist OR has_evolve_pubkey)
  const sel = _onboardSelectedBots();
  if (!sel.length) {
    document.getElementById('onboard-submit-btn').disabled = true;
    return;
  }
  const provider = _onboardState.provider;
  for (const b of sel) {
    const st = _onboardState.perBot[b];
    if (!st || !st.verify || !st.verify.ok) {
      document.getElementById('onboard-submit-btn').disabled = true;
      return;
    }
    if (provider === 'github') {
      const repo = st.verify.repo || {};
      if (repo.exists && !repo.has_evolve_pubkey && !st.reuse_confirmed) {
        document.getElementById('onboard-submit-btn').disabled = true;
        return;
      }
    } else if (provider === 'brave') {
      // Brave: opted-out bots can be selected but they'll go to skipped.
      // Submit stays enabled regardless.
    }
  }
  const btn = document.getElementById('onboard-submit-btn');
  btn.disabled = false;
  btn.textContent = `▶ Set Up Across ${sel.length} Bot${sel.length === 1 ? '' : 's'}`;
}

async function onboardVerify() {
  const provider = _onboardState.provider;
  const cred = document.getElementById('onboard-cred-input').value.trim();
  if (!cred) {
    document.getElementById('onboard-cred-status').innerHTML = `<span style="color:var(--red)">Paste a credential first.</span>`;
    return;
  }
  const sel = _onboardSelectedBots();
  if (!sel.length) {
    document.getElementById('onboard-cred-status').innerHTML = `<span style="color:var(--red)">Select at least one bot.</span>`;
    return;
  }
  document.getElementById('onboard-cred-status').innerHTML = `<span class="loading"><span class="spinner"></span> Verifying…</span>`;

  if (provider === 'github') {
    const body = {
      default: { token: cred, github_login: _onboardState.discoveredLogin || '' },
      bots: sel.map(b => {
        const st = _onboardState.perBot[b] || {};
        const entry = { bot_id: b, repo_name: st.repo_name || `${b}-workspace` };
        // Per-bot org selection: when set, the backend uses this login for
        // the /repos/{login}/{repo} probe instead of the cascade default.
        if (st.github_login) entry.github_login = st.github_login;
        if (st.override_open && st.override_token) {
          entry.override = { token: st.override_token, github_login: st.override_login || '' };
        }
        return entry;
      }),
    };
    const r = await api('POST', '/api/admin/onboard/github/verify', body);
    if (!r || r.error) {
      document.getElementById('onboard-cred-status').innerHTML = `<span style="color:var(--red)">${escHtml((r && r.error) || 'verify failed')}</span>`;
      return;
    }
    // Refresh availableOrgs with what the user-pasted PAT actually has access
    // to — the discover-default-pat list is the cascade-discovered PAT's view,
    // which may differ from the operator's pasted PAT.
    if (Array.isArray(r.available_orgs) && r.available_orgs.length) {
      _onboardState.availableOrgs = r.available_orgs;
    }
    // Pick a representative login for the cred-status line: first per-bot ok login.
    const repBot = (r.bots || []).find(b => b.ok && b.login);
    if (repBot) {
      _onboardState.verifiedLogin = repBot.login;
      _onboardState.verifiedFineGrained = !!repBot.fine_grained;
      document.getElementById('onboard-cred-status').innerHTML =
        `<span style="color:var(--green)">✓ Verified — GitHub user: <strong>${escHtml(repBot.login)}</strong>${repBot.fine_grained ? ' (fine-grained)' : ''}</span>`;
    } else {
      document.getElementById('onboard-cred-status').innerHTML = `<span style="color:var(--red)">No bot verified successfully — check token or scopes.</span>`;
    }
    // Stash per-bot results
    for (const b of (r.bots || [])) {
      if (_onboardState.perBot[b.bot_id]) {
        _onboardState.perBot[b.bot_id].verify = b;
      }
    }
  } else if (provider === 'brave') {
    const r = await api('POST', '/api/admin/onboard/brave/verify', { key: cred, bots: sel });
    if (!r || r.error) {
      document.getElementById('onboard-cred-status').innerHTML = `<span style="color:var(--red)">${escHtml((r && r.error) || 'verify failed')}</span>`;
      return;
    }
    if (!r.brave_ok) {
      document.getElementById('onboard-cred-status').innerHTML = `<span style="color:var(--red)">✗ Key rejected (status ${r.status})</span>`;
    } else {
      document.getElementById('onboard-cred-status').innerHTML = `<span style="color:var(--green)">✓ Brave key verified</span>`;
    }
    for (const b of (r.bots || [])) {
      if (_onboardState.perBot[b.bot_id]) {
        _onboardState.perBot[b.bot_id].verify = { ok: true, ...b };
      }
    }
  }
  _onboardRenderBotsList();
  _onboardUpdateSubmitState();
}

async function submitOnboard() {
  const provider = _onboardState.provider;
  const sel = _onboardSelectedBots();
  if (!sel.length) return;
  const cred = document.getElementById('onboard-cred-input').value.trim();
  document.getElementById('onboard-result').innerHTML = `<span class="loading"><span class="spinner"></span> Setting up across ${sel.length} bot${sel.length === 1 ? '' : 's'}…</span>`;
  document.getElementById('onboard-submit-btn').disabled = true;

  if (provider === 'github') {
    const body = {
      default: { token: cred, github_login: _onboardState.verifiedLogin || _onboardState.discoveredLogin || '' },
      bots: sel.map(b => {
        const st = _onboardState.perBot[b] || {};
        const entry = {
          bot_id: b,
          repo_name: st.repo_name || `${b}-workspace`,
          reuse_confirmed: !!st.reuse_confirmed,
        };
        if (st.github_login) entry.github_login = st.github_login;
        if (st.override_open && st.override_token) {
          entry.override = { token: st.override_token, github_login: st.override_login || '' };
        }
        return entry;
      }),
    };
    const r = await api('POST', '/api/admin/onboard/github', body);
    _onboardRenderResult(r, 'github');
  } else if (provider === 'brave') {
    const r = await api('POST', '/api/admin/onboard/brave', { key: cred, bots: sel });
    _onboardRenderResult(r, 'brave');
  }
  // Refresh keys so the dashboard reflects the new state.
  loadKeys();
}

function _onboardRenderResult(r, provider) {
  const el = document.getElementById('onboard-result');
  if (!r || (r.error && !r.results)) {
    if (r && r.unresolved && Array.isArray(r.unresolved) && r.unresolved.length) {
      // Each [Reuse] / [Rename] is a clickable affordance so the operator
      // doesn't have to scroll the bot list to find the right row.
      // Reuse flips reuse_confirmed inline; Rename just scrolls to the
      // row and highlights it so the operator can edit the repo_name
      // field in place.
      const lines = r.unresolved.map(u => `⚠ <strong>${escHtml(u.bot)}</strong>: <code>${escHtml(u.repo)}</code> already exists — <a href="javascript:void(0)" onclick="onboardConfirmReuseFromBanner(${JSON.stringify(u.bot).replace(/"/g, '&quot;')})" style="font-weight:600">[Reuse this repo]</a> or <a href="javascript:void(0)" onclick="onboardScrollToRow(${JSON.stringify(u.bot).replace(/"/g, '&quot;')})">[Rename]</a>.`).join('<br>');
      el.innerHTML = `<div class="alert alert-error">Unresolved collisions:<br>${lines}</div>`;
      _onboardUpdateSubmitState();
    } else {
      el.innerHTML = `<div class="alert alert-error">${escHtml((r && r.error) || 'request failed')}</div>`;
      _onboardUpdateSubmitState();
    }
    return;
  }
  const results = r.results || [];
  const skipped = r.skipped || [];
  const lines = results.map(x => {
    if (x.ok) {
      const note = x.repo_reused ? 'reused' : (x.repo_url ? `created ${x.repo_url}` : 'ok');
      const dk = x.deploy_key_added ? ', deploy key registered' : '';
      const overrideNote = x.provider_overridden ? ` (provider unchanged: ${escHtml(x.current_provider)})` : '';
      return `✓ ${escHtml(x.bot)} — ${note}${dk}${overrideNote}`;
    }
    return `⚠ ${escHtml(x.bot)} — ${escHtml(x.error || 'failed')}`;
  });
  for (const s of skipped) {
    lines.push(`⊘ ${escHtml(s.bot)} — skipped (${escHtml(s.reason)})`);
  }
  el.innerHTML = `<div class="alert alert-ok">${lines.join('<br>')}</div>`;
  // Run is finished — relabel Cancel → Close and hide the now-pointless
  // disabled Set-Up button so the modal reads as "done, click to dismiss"
  // instead of "you must cancel a thing that already succeeded".
  document.getElementById('onboard-cancel-btn').textContent = 'Close';
  document.getElementById('onboard-submit-btn').style.display = 'none';
  // For brave, prompt for restart if any result requires it.
  if (provider === 'brave') {
    const needsRestart = results.find(x => x.ok && x.requires_restart && x.restart_endpoint);
    if (needsRestart) {
      setTimeout(() => _promptGatewayRestart(needsRestart.bot, needsRestart.restart_endpoint), 1200);
    }
  }
}

// ── Model Catalog ──────────────────────────────────────────────────────────
let _mcEditingModel = null; // null = new, string = existing model id being edited


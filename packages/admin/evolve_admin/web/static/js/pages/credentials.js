// ════════════════════════════════════════════════════════════════════════
// Page: Credentials subtab (Plugins page) — row renderer + add-key wizard
//
// The Credentials subtab of the Plugins page (formerly Integrations &
// Keys). This file contains the per-credential row renderer
// (ikRenderKeys), the add-key wizard modal, key rotation, and the
// gateway restart prompt that fires after rotation.
//
// Cluster contents:
//   loadKeys()                  — compat alias for ikRenderKeys(_ikBot)
//   _renderWarningChip(row)     — yellow chip for credential warnings
//   _renderActions(row, botId)  — per-row action menu (View / Rotate / etc.)
//   _dispatchAction(...)        — action menu dispatcher
//   openConfigViewModal / closeConfigViewModal — view-only config modal
//   ikRenderKeys(botId)         — the main per-bot credential rows render
//   _ikRenderOnboardBanner()    — the onboarding banner above the rows
//
//   Add-key wizard:
//     openAddKeyModal(provider, keyType)
//     _updateAddKeyTypeVisibility()
//     _updateAddKeySourceMode()
//     _updateAddKeyBorrowOptions()
//     closeAddKeyModal()
//     submitAddKey()
//
//   Key rotation + gateway restart:
//     submitRotateKey()
//     _promptGatewayRestart(botId, restartEndpoint)
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - api(), toast(), escHtml(), botLabel() — core/
//   - _ikBot — pages/plugins.js (Phase 3z; the bot selector global)
//   - nav() / window.nav — core/router.js
//   - loadStatus() (Overview) — refreshed after credential changes
//   - The substrate-audit chip helpers — pages/skills.js (Phase 3r)
//     (called by ikRenderKeys's OAuth row branch via global lookup)
// ════════════════════════════════════════════════════════════════════════

async function loadKeys() { await ikRenderKeys(_ikBot); } // compat alias

// ── Q5/Q2: warning chips ─────────────────────────────────────────────────
// Each row may carry a `warnings: [...]` field populated server-side. Two
// kinds today, both yellow but with different short-labels so an operator
// can tell them apart at a glance:
//
//   - probe ERROR (Q5, default kind): a probe found storage that appeared
//     to exist but couldn't be read — typically a sudoers-grant misconfig
//     hiding a working integration.
//   - manifest_without_credentials (Q2): the bot's workspace declares a
//     manifest matching this provider, but no probe found credentials —
//     intent declared, no creds. Operator should authorize or remove.
//
// Both render as informational-yellow next to the status badge; the label
// distinguishes them. Multi-warning rows collapse to a count and rely on
// the hover for detail.
function _renderWarningChip(row) {
  const warns = (row && row.warnings) || [];
  if (!warns.length) return '';
  const lines = warns.map(w => {
    const probe = w && w.probe_name ? `[${w.probe_name}] ` : '';
    const reason = w && w.reason ? w.reason : 'probe error';
    const hint = w && w.remediation_hint ? ` — ${w.remediation_hint}` : '';
    return `${probe}${reason}${hint}`;
  });
  let label;
  if (warns.length === 1) {
    const w = warns[0];
    if (w && w.kind === 'manifest_without_credentials') {
      label = '⚠ Intent without credentials';
    } else {
      const reason = String(w.reason || 'probe error');
      label = `⚠ ${escHtml(reason.slice(0, 60))}${reason.length > 60 ? '…' : ''}`;
    }
  } else {
    // Mixed-kind multi-warning row: lead with the most actionable count.
    const errCount = warns.filter(w => !w || w.kind !== 'manifest_without_credentials').length;
    const intentCount = warns.length - errCount;
    if (errCount && intentCount) {
      label = `⚠ ${errCount} read error${errCount === 1 ? '' : 's'} + intent`;
    } else if (intentCount) {
      label = `⚠ ${intentCount} intent without creds`;
    } else {
      label = `⚠ ${errCount} probes errored`;
    }
  }
  const title = lines.join('\n');
  return ` <span class="probe-warning-chip" title="${escHtml(title)}"
    style="display:inline-block;padding:1px 6px;background:rgba(251,191,36,0.12);border:1px solid var(--yellow);border-radius:10px;font-size:0.7rem;margin-left:6px;color:var(--yellow);cursor:help;vertical-align:middle">${label}</span>`;
}

// ── Phase 3: affordance routing ─────────────────────────────────────────
// _renderActions reads row.actions[] (populated server-side from the
// winning probe's affordances) and emits the buttons. Adding a new
// provider with a custom action shape is a probe-side concern: register
// the probe with the right affordances, map them in server.py's
// _action_from_affordance, and this renderer picks them up. No
// per-provider branches here.
function _renderActions(row, botId) {
  const acts = (row && row.actions) || [];
  if (!acts.length) {
    return '<span style="color:var(--text3);font-size:0.75rem">—</span>';
  }
  const q = v => JSON.stringify(v).replace(/"/g, '&quot;');
  return acts.map(a => {
    let cls = 'btn-ghost';
    let extraStyle = '';
    if (a.style === 'danger') {
      extraStyle = 'color:var(--red)';
    } else if (a.style === 'warn') {
      extraStyle = 'border:1px solid var(--orange);color:var(--orange)';
    } else if (a.style === 'primary') {
      cls = 'btn-primary';
    }
    const onclick = `_dispatchAction(${q(a)},${q(row)},${q(botId)})`;
    const label = escHtml(a.label || a.id || '');
    const style = extraStyle ? ` style="${extraStyle}"` : '';
    return `<button class="btn ${cls} btn-sm"${style} onclick="${onclick}">${label}</button>`;
  }).join(' ');
}

async function _dispatchAction(action, row, botId) {
  if (!action || !action.id) return;
  if (action.modal === 'rotate_modal') {
    const lbl = `${row.provider} ${row.type_label || row.type || ''}`.trim();
    openRotateKeyModal(botId, row.profile_id, row.provider, lbl, action.endpoint || null, null, null, row.storage || null);
    return;
  }
  if (action.modal === 'google_workspace_wizard') {
    // Fresh add ("Set up", status=missing) → the unified account-type chooser
    // so the operator can pick Workspace (path C / service-account + DwD) vs
    // Personal (OAuth). This keeps every FRESH Google add — Skills page AND
    // here — on the ONE chooser, so Path C stays reachable from both surfaces.
    // Reauthorize/migrate act on an EXISTING install whose account type is
    // already determined — re-run that wizard directly (no chooser).
    if (action.id === 'setup' && typeof openGoogleUnifiedChooser === 'function') {
      openGoogleUnifiedChooser(botId, 'google');
    } else {
      openGoogleWorkspaceWizard(botId);
    }
    return;
  }
  if (action.modal === 'config_view') {
    openConfigViewModal(botId, row.provider, row.display || row.provider);
    return;
  }
  if (action.id === 'disconnect') {
    if (!action.endpoint) return;
    const label = row.display || row.provider || 'integration';
    if (!await confirmModal({body: `Disconnect ${label} from ${botLabel(botId)}? The bot will lose access immediately.`, danger: true})) return;
    const r = await api('POST', action.endpoint, { bot_id: botId });
    if (!r || r.error) {
      toast('Disconnect failed: ' + ((r && r.error) || 'unknown'), 'err');
      return;
    }
    toast(r.revoked === false ? 'Disconnected (remote revoke skipped).' : 'Disconnected.', 'ok');
    if (typeof loadKeys === 'function') loadKeys();
    return;
  }
  // Fallback — unknown action; surface to operator instead of silently
  // doing nothing.
  toast(`Action "${action.id}" not yet wired`, 'err');
}

async function openConfigViewModal(botId, provider, providerLabel) {
  const modal = document.getElementById('config-view-modal');
  const titleEl = document.getElementById('config-view-title');
  const pathEl = document.getElementById('config-view-path');
  const bodyEl = document.getElementById('config-view-body');
  const noteEl = document.getElementById('config-view-note');
  if (!modal || !titleEl || !pathEl || !bodyEl || !noteEl) return;
  titleEl.textContent = `View config — ${providerLabel}`;
  pathEl.textContent = 'Loading…';
  bodyEl.textContent = '';
  noteEl.textContent = '';
  modal.classList.add('open');
  const r = await api('GET', `/api/admin/keys/${botId}/${provider}/config`);
  if (!r || r.error) {
    pathEl.textContent = '';
    bodyEl.textContent = (r && r.error) || 'Failed to load config';
    return;
  }
  pathEl.innerHTML = `<code>${escHtml(r.openclaw_json_path || '')}</code> · path: <code>${escHtml(r.path || '')}</code>`;
  if (r.json_fragment === null || r.json_fragment === undefined) {
    bodyEl.textContent = '(not configured at this path)';
  } else {
    bodyEl.textContent = JSON.stringify(r.json_fragment, null, 2);
  }
  const masked = (r.masked_fields || []).filter(f => bodyEl.textContent.indexOf(`"${f}"`) !== -1);
  noteEl.textContent = masked.length
    ? `Read-only view. Secret fields are masked: ${masked.join(', ')}.`
    : 'Read-only view. To change anything, edit openclaw.json on the bot host.';
}

function closeConfigViewModal() {
  const modal = document.getElementById('config-view-modal');
  if (modal) modal.classList.remove('open');
}

async function ikRenderKeys(botId) {
  const el = document.getElementById('ik-keys-table');
  if (!el) return;
  if (!botId) { el.innerHTML = '<div class="empty">No bots configured.</div>'; return; }
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  const d = await api('GET', `/api/admin/keys/${botId}`);
  if (d.error) {
    el.innerHTML = `<div style="padding:12px;color:var(--red)">${escHtml(d.error)}</div>`;
    return;
  }
  const keys = d.keys || [];
  const podInvariants = Array.isArray(d.pod_invariants) ? d.pod_invariants : [];
  const podDefaultGithubAccount = d.pod_default_github_account || null;
  const podDefaultGithubAccountSource = d.pod_default_github_account_source || null;
  const sourceNote = d.source_path
    ? `<div style="font-size:0.72rem;color:var(--text3);margin-bottom:8px">Source: <code>${escHtml(d.source_path)}</code></div>`
    : `<div style="font-size:0.72rem;color:var(--orange);margin-bottom:8px">⚠ auth-profiles.json not found — add a key to create it</div>`;

  const CAT_LABELS = {
    llm: 'LLM Providers', messaging: 'Messaging', media: 'Media / Creative AI',
    search: 'Search', storage: 'Storage', workspace: 'Google Workspace', custom: 'Custom'
  };
  const CAT_ORDER = ['llm', 'messaging', 'media', 'search', 'storage', 'workspace', 'custom'];

  // Visibility: honor the backend's authoritative `should_list` boolean —
  // the "hide until configured OR attempted" rule lives in ONE place
  // (routes_admin.py::api_admin_get_keys). A row is pre-listed only when it
  // has a real credential, is a pod-invariant provider (so the operator sees
  // the gap and gets a "▶ Set up" button), or carries a setup-attempt signal
  // (a probe/manifest warning). An optional, never-touched provider (Slack,
  // Dropbox, Google Workspace on a fresh pod) is NOT pre-listed — it stays
  // fully reachable via "+ Add Key". OAuth providers obey the same rule.
  //
  // Fallback (older backend without `should_list`): replicate the rule
  // client-side, but WITHOUT the legacy `!isOauth` exemption that used to
  // force every missing OAuth row to render.
  const byCategory = {};
  for (const k of keys) {
    let show;
    if (typeof k.should_list === 'boolean') {
      show = k.should_list;
    } else {
      const isPodInvariant = podInvariants.includes(k.provider);
      // Q5: even a "missing" non-invariant row gets shown when probes
      // errored — silently dropping the row would re-create the
      // team_bot_a/team_bot_c failure mode where "couldn't read" looked
      // identical to "not configured". Warnings imply something on disk we
      // couldn't read.
      const hasWarnings = Array.isArray(k.warnings) && k.warnings.length > 0;
      show = !(k.status === 'missing' && !isPodInvariant && !hasWarnings);
    }
    if (!show) continue;
    const cat = k.category || 'custom';
    byCategory[cat] = byCategory[cat] || [];
    byCategory[cat].push(k);
  }

  const _keyRow = (k) => {
    const cc = k.credential_class || 'api_key';
    const missing = k.status === 'missing';
    const _optedOut = k.status === 'opted_out';
    const display = escHtml(k.display || k.provider || '—');
    const typeLabel = escHtml(k.type_label || k.type || '—');
    const rowStyle = (missing || _optedOut) ? ' style="opacity:0.6"' : '';
    // data-provider drives the scroll-to-row affordance used by
    // ikJumpToCredentials() — when an Embeddings or Plugins row hands off
    // "Rotate in Credentials →", we land on this tab and highlight the
    // matching row. Lowercase to match the case used by the jump caller.
    const dp = `data-provider="${escHtml((k.provider || '').toLowerCase())}"`;
    // q(): JSON-serialize a value safe for use inside an onclick="" attribute.
    // JSON.stringify produces double-quoted strings which would break the
    // surrounding attribute quotes — &quot; is decoded to " by the HTML
    // parser before the JS engine sees it, so the handler receives the
    // correct string value.
    const q = v => JSON.stringify(v).replace(/"/g, '&quot;');

    const warningChip = _renderWarningChip(k);

    if (cc === 'integration_token') {
      const maskedCell = k.masked ? `<code style="font-size:0.78rem">${escHtml(k.masked)}</code>` : '<span style="color:var(--text3)">—</span>';
      const rotateEndpoint = `/api/admin/integration-token/${botId}/${k.provider}/rotate`;

      // ── Discord branch ───────────────────────────────────────────────
      // channels.discord.token lives in openclaw.json. Three states map to
      // three different action buttons (no silent no-op):
      //   - missing:        Set up (opens rotate modal pointed at rotate endpoint)
      //   - single account: Rotate (validates against discord.com/users/@me)
      //   - multi-account:  Disabled info button (rotate route returns 400)
      if (k.provider === 'discord') {
        if (missing) {
          const help = k.setup_help ? `<div style="font-size:0.7rem;color:var(--text3);margin-top:3px">${escHtml(k.setup_help)}</div>` : '';
          const setupBtn = `<button class="btn btn-ghost btn-sm" style="border:1px solid var(--orange);color:var(--orange)"
            onclick="openRotateKeyModal(${q(botId)},${q(k.profile_id)},${q(k.provider)},'Discord bot token',${q(rotateEndpoint)})">▶ Set up</button>`;
          return `<tr ${dp} style="opacity:0.85">
            <td><strong>${display}</strong></td>
            <td style="font-size:0.8rem;color:var(--text2)">${typeLabel}${help}</td>
            <td>${maskedCell}</td>
            <td><span style="color:var(--orange)">⚠ Setup required</span>${warningChip}</td>
            <td>—</td>
            <td style="text-align:right">${setupBtn}</td>
          </tr>`;
        }
        let dcBtn;
        if (k.discord_shape === 'multi_account') {
          const accts = (k.discord_accounts || []).join(', ');
          const tip = `This bot has channels.discord.accounts populated (${accts}). Rotating from this UI would only touch the default account's token, which could silently disconnect a sub-account. Edit openclaw.json directly.`;
          dcBtn = `<button class="btn btn-ghost btn-sm" disabled title="${escHtml(tip)}" style="cursor:help">ⓘ Multi-account</button>`;
        } else {
          dcBtn = `<button class="btn btn-ghost btn-sm"
            onclick="openRotateKeyModal(${q(botId)},${q(k.profile_id)},${q(k.provider)},'Discord bot token',${q(rotateEndpoint)})">↺ Rotate</button>`;
        }
        const enabledNote = k.discord_enabled === false
          ? ' <span style="font-size:0.7rem;color:var(--orange)">(disabled in openclaw.json)</span>'
          : '';
        const guildNote = (k.discord_shape === 'single' && typeof k.discord_guild_count === 'number')
          ? ` <span style="font-size:0.7rem;color:var(--text3)">— ${k.discord_guild_count} guild${k.discord_guild_count === 1 ? '' : 's'}</span>`
          : '';
        return `<tr ${dp}>
          <td><strong>${display}</strong></td>
          <td style="font-size:0.8rem;color:var(--text2)">${typeLabel}</td>
          <td>${maskedCell}</td>
          <td><span style="color:var(--green)">✅ Active</span>${enabledNote}${guildNote}${warningChip}</td>
          <td>—</td>
          <td style="text-align:right">${dcBtn}</td>
        </tr>`;
      }

      // ── WhatsApp branch ──────────────────────────────────────────────
      // No rotatable token exists — Baileys uses session-based auth via QR
      // pairing on the bot host. Actions never claim a side effect they
      // can't deliver:
      //   - missing:             Set up (POSTs to /whatsapp/setup, scaffolds + enables)
      //   - configured_disabled: Enable button (POSTs to /whatsapp/setup with enabled=true)
      //   - active:              Disabled info button (rotate not applicable)
      if (k.provider === 'whatsapp') {
        const setupEndpoint = `/api/admin/integration-token/${botId}/whatsapp/setup`;
        const pairTip = `WhatsApp uses Baileys session-based auth — no token to rotate. To re-pair: ssh to the bot host as the bot user and run \`oc whatsapp pair\` to scan a fresh QR code.`;
        if (missing) {
          const help = k.setup_help ? `<div style="font-size:0.7rem;color:var(--text3);margin-top:3px">${escHtml(k.setup_help)}</div>` : '';
          const setupBtn = `<button class="btn btn-ghost btn-sm" style="border:1px solid var(--orange);color:var(--orange)"
            onclick="whatsappSetup(${q(botId)},true)">▶ Set up</button>`;
          return `<tr ${dp} style="opacity:0.85">
            <td><strong>${display}</strong></td>
            <td style="font-size:0.8rem;color:var(--text2)">${typeLabel}${help}</td>
            <td>${maskedCell}</td>
            <td><span style="color:var(--orange)">⚠ Setup required</span>${warningChip}</td>
            <td>—</td>
            <td style="text-align:right">${setupBtn}</td>
          </tr>`;
        }
        let waActions;
        if (k.status === 'configured_disabled') {
          waActions = `<button class="btn btn-ghost btn-sm" style="border:1px solid var(--orange);color:var(--orange)"
            onclick="whatsappSetup(${q(botId)},true)">▶ Enable</button>`;
        } else {
          waActions = `<button class="btn btn-ghost btn-sm" disabled title="${escHtml(pairTip)}" style="cursor:help">ⓘ Pair via CLI</button>` +
            ` <button class="btn btn-ghost btn-sm" style="color:var(--text3)" onclick="whatsappSetup(${q(botId)},false)" title="Disable channels.whatsapp.enabled in openclaw.json (does not delete Baileys session files)">Disable</button>`;
        }
        const statusBadge = k.status === 'configured_disabled'
          ? '<span style="color:var(--text3)">⊘ Configured (disabled)</span>'
          : '<span style="color:var(--green)">✅ Enabled</span>';
        return `<tr ${dp}>
          <td><strong>${display}</strong></td>
          <td style="font-size:0.8rem;color:var(--text2)">${typeLabel}</td>
          <td>${maskedCell}</td>
          <td>${statusBadge}${warningChip}</td>
          <td>—</td>
          <td style="text-align:right;white-space:nowrap">${waActions}</td>
        </tr>`;
      }

      // ── GitHub branch (existing) ─────────────────────────────────────
      // Per-bot github account override chip: render only when the bot's
      // discovered repo owner differs from the pod default.
      let overrideChip = '';
      if (k.provider === 'github' && k.repo_slug && podDefaultGithubAccount) {
        const owner = String(k.repo_slug).split('/')[0];
        if (owner && owner !== podDefaultGithubAccount) {
          const sourceLabel = podDefaultGithubAccountSource
            ? `, inferred from ${podDefaultGithubAccountSource.replace('discovered_from_', '')}`
            : '';
          const tooltip = `This bot's github backup is under ${owner}, not the pod default ${podDefaultGithubAccount}${sourceLabel}. Click to view in onboard modal.`;
          overrideChip = ` <button class="btn btn-ghost btn-sm" style="padding:1px 6px;font-size:0.72rem;border:1px solid var(--orange);color:var(--orange)"
            title="${escHtml(tooltip)}"
            onclick="openOnboardModal('github',[${q(botId)}],${q(owner)})">⤴ ${escHtml(owner)}</button>`;
        }
      }
      // Missing pod-invariant github row: surface a Set up button instead of Rotate
      if (missing) {
        const setupBtn = `<button class="btn btn-ghost btn-sm" style="border:1px solid var(--orange);color:var(--orange)"
          onclick="openOnboardModal('github',[${q(botId)}])">▶ Set up</button>`;
        return `<tr ${dp} style="opacity:0.85">
          <td><strong>${display}</strong></td>
          <td style="font-size:0.8rem;color:var(--text2)">${typeLabel}</td>
          <td>${maskedCell}</td>
          <td><span style="color:var(--orange)">⚠ Setup required</span>${warningChip}</td>
          <td>—</td>
          <td style="text-align:right">${setupBtn}</td>
        </tr>`;
      }
      // Action button varies by github auth_type so we never render a
      // silent no-op:
      //   - https_pat:        Rotate (replaces the embedded token)
      //   - https_credhelper: Rotate, with tooltip noting the auth-model switch
      //   - ssh:              Info button (no rotation through this UI)
      let rotateBtn;
      if (k.provider === 'github' && k.auth_type === 'ssh') {
        const sshTip = `SSH deploy key — to rotate, regenerate /Users/evolve/.ssh/evolve-backup-${botId} and update the deploy key on the repo's GitHub Settings → Deploy keys page. Rotation isn't supported from this UI.`;
        rotateBtn = `<button class="btn btn-ghost btn-sm" disabled
          title="${escHtml(sshTip)}"
          style="cursor:help">ⓘ How to rotate</button>`;
      } else if (k.provider === 'github' && k.auth_type === 'https_credhelper') {
        const tip = `This bot uses a credential helper. Rotating will switch it to URL-embedded PAT auth so future rotations work in-place.`;
        rotateBtn = `<button class="btn btn-ghost btn-sm"
          title="${escHtml(tip)}"
          onclick="openRotateKeyModal(${q(botId)},${q(k.profile_id)},${q(k.provider)},${q(k.provider + ' ' + k.type_label)},${q(rotateEndpoint)},null,${q('credhelper_to_pat')})">↺ Rotate</button>`;
      } else {
        rotateBtn = `<button class="btn btn-ghost btn-sm"
          onclick="openRotateKeyModal(${q(botId)},${q(k.profile_id)},${q(k.provider)},${q(k.provider + ' ' + k.type_label)},${q(rotateEndpoint)})">↺ Rotate</button>`;
      }
      const _itAlerts = parseApiErrors(_gwCachedErrors(botId)).filter(a => a.provider === k.provider);
      const _itErrBadge = _itAlerts.length ? ` ${renderErrorLabels(_itAlerts)}` : '';
      return `<tr ${dp}>
        <td><strong>${display}</strong></td>
        <td style="font-size:0.8rem;color:var(--text2)">${typeLabel}</td>
        <td>${maskedCell}</td>
        <td><span style="color:var(--green)">✅ Active</span>${overrideChip}${_itErrBadge}${warningChip}</td>
        <td>—</td>
        <td style="text-align:right">${rotateBtn}</td>
      </tr>`;
    }

    if (cc === 'oauth') {
      // Status + display formatting stays here (scope chips, account email,
      // dropbox sync path) — these describe the row's *content*, not its
      // affordances. Action buttons come exclusively from row.actions, which
      // the keys API derives from the winning probe's affordances. Adding a
      // new OAuth provider doesn't require an `if (provider === ...)` branch
      // in this code path; register the probe with the right affordances and
      // the renderer picks them up.
      const status = k.status || 'missing';
      const isPluginManaged = status === 'active' && k.oc_only && k.flavor === 'plugin-managed';
      const isLegacyGoogle = status === 'active' && k.oc_only && !isPluginManaged && k.provider === 'google_workspace';
      const isDesktopDropbox = status === 'active' && k.oc_only && k.provider === 'dropbox';

      let statusIcon, statusColor;
      if (status === 'active') {
        if (isPluginManaged) {
          statusIcon = '✅ Authorized (plugin-managed)'; statusColor = 'var(--green)';
        } else if (isLegacyGoogle) {
          statusIcon = '✅ Authorized (legacy)'; statusColor = 'var(--green)';
        } else if (isDesktopDropbox) {
          statusIcon = '✅ Connected (Dropbox app)'; statusColor = 'var(--green)';
        } else {
          statusIcon = '✅ Authorized'; statusColor = 'var(--green)';
        }
      } else if (status === 'reauth_required' || status === 'expired') {
        statusIcon = '⚠️ Re-authorization required'; statusColor = 'var(--orange)';
      } else {
        statusIcon = '❌ Not connected'; statusColor = 'var(--text3)';
      }
      const info = k.oauth_info ? `<div style="font-size:0.72rem;color:var(--text3);margin-top:3px">${escHtml(k.oauth_info)}</div>` : '';

      // Display extras: scope chips / account email / dropbox sync path /
      // plugin-managed credential summary. None of these gate any action.
      let extra = '';
      if (status === 'active' && k.provider === 'google_workspace') {
        const labels = {gmail: 'gmail', calendar: 'calendar', drive: 'drive', docs: 'docs', sheets: 'sheets', slides: 'slides', gmail_modify: 'gmail*', drive_full: 'drive*'};
        const hasChips = Array.isArray(k.granted_services) && k.granted_services.length;
        const chips = hasChips
          ? k.granted_services.map(s => `<span style="display:inline-block;padding:1px 6px;background:var(--bg2);border:1px solid var(--border);border-radius:10px;font-size:0.7rem;margin-right:4px">${escHtml(labels[s] || s)}</span>`).join('')
          : (isLegacyGoogle ? '<span style="font-size:0.72rem;color:var(--text3)">scopes unknown — Reauthorize to inventory</span>' : '');
        const acct = k.google_account ? `<div style="font-size:0.72rem;color:var(--text2);margin-top:3px">${escHtml(k.google_account)}</div>` : '';
        const legacyNote = isLegacyGoogle
          ? `<div style="font-size:0.7rem;color:var(--orange);margin-top:3px">Connected via the legacy <code>oc gws</code> CLI. Click Reauthorize to migrate to the wizard-managed flow${k.legacy_token_age_days != null ? ` (token last refreshed ${k.legacy_token_age_days}d ago)` : ''}.</div>`
          : '';
        let pluginNote = '';
        if (isPluginManaged) {
          const sum = k.plugin_credential_summary || {};
          const parts = [];
          if (sum.token_caches) parts.push(`${sum.token_caches} token cache${sum.token_caches === 1 ? '' : 's'}`);
          if (sum.service_accounts) parts.push(`${sum.service_accounts} service account${sum.service_accounts === 1 ? '' : 's'}`);
          if (sum.client_secrets) parts.push(`${sum.client_secrets} client secret${sum.client_secrets === 1 ? '' : 's'}`);
          const summary = parts.length ? ` · ${parts.join(' · ')}` : '';
          const manifestNote = (k.manifest_files && k.manifest_files.length)
            ? `<div style="font-size:0.7rem;color:var(--text3);margin-top:2px">Manifest: <code>${k.manifest_files.map(escHtml).join(', ')}</code></div>`
            : '';
          pluginNote = `<div style="font-size:0.7rem;color:var(--text3);margin-top:3px">Credentials in <code>~/.openclaw/workspace/credentials/</code>${summary}. The running plugin reads tokens from disk; the dashboard does not write here (decision B).</div>${manifestNote}`;
        }
        if (hasChips || isLegacyGoogle || isPluginManaged) {
          extra = `<div style="margin-top:4px">${chips}</div>${acct}${legacyNote}${pluginNote}`;
        } else if (k.google_account) {
          extra = acct;
        }
      } else if (isDesktopDropbox) {
        const path = k.dropbox_sync_path ? `<div style="font-size:0.72rem;color:var(--text2);margin-top:3px"><code>${escHtml(k.dropbox_sync_path)}</code></div>` : '';
        const sub = k.dropbox_subscription_type
          ? `<span style="display:inline-block;padding:1px 6px;background:var(--bg2);border:1px solid var(--border);border-radius:10px;font-size:0.7rem;margin-right:4px">${escHtml(k.dropbox_subscription_type)}${k.dropbox_is_team ? ' · team' : ''}</span>`
          : '';
        const note = `<div style="font-size:0.7rem;color:var(--text3);margin-top:3px">Connected via the macOS Dropbox app under the bot's user account. To change accounts, sign out and back in from the Dropbox app on the mini.</div>`;
        extra = `${sub ? `<div style="margin-top:4px">${sub}</div>` : ''}${path}${note}`;
      }

      const actions = _renderActions(k, botId);
      // Audit affordances — only render for active providers; an
      // inactive provider has nothing to audit. Spec: audit-extensions §4.2.
      const auditCluster = (status === 'active')
        ? _renderSubstrateAuditRows('provider', k.provider, [botId])
        : '';
      return `<tr ${dp}${rowStyle}>
        <td><strong>${display}</strong></td>
        <td style="font-size:0.8rem;color:var(--text2)">${typeLabel}</td>
        <td colspan="2"><span style="color:${statusColor}">${statusIcon}</span>${warningChip}${extra}${info}${auditCluster}</td>
        <td>—</td>
        <td style="text-align:right;white-space:nowrap">${actions}</td>
      </tr>`;
    }

    if (cc === 'token_pair') {
      const fields = k.fields || [];
      const anyActive = fields.some(f => f.status === 'active');
      const pairInfo = k.pair_info ? `<div style="font-size:0.72rem;color:var(--text3);margin-top:3px">${escHtml(k.pair_info)}</div>` : '';
      // Storage chip — operators need to know what they're rotating before
      // they hit the button. The probe cascade reports k.storage as one of
      // "auth_profiles", "openclaw_channels", or "dotenv"; map each to a
      // human label that names the file the rotate path would write to.
      const storageChipMap = {
        auth_profiles: 'auth-profiles.json',
        openclaw_channels: 'openclaw.json · channels.' + k.provider,
        dotenv: 'workspace/.env',
      };
      const storageLabel = storageChipMap[k.storage] || null;
      const storageChip = storageLabel
        ? `<span title="Probe winner — the rotate button writes here" style="display:inline-block;padding:1px 6px;background:var(--bg2);border:1px solid var(--border);border-radius:10px;font-size:0.7rem;margin-right:6px;color:var(--text2)">📍 stored in <code>${escHtml(storageLabel)}</code></span>`
        : '';
      // Disable the rotate button on storage shapes we haven't wired up
      // (today: dotenv-only). The endpoint would fail and the runtime read
      // path is unverified — better to surface the chip than to break
      // decision B by writing somewhere the runtime might not read from.
      const rotatableStorage = (k.storage === 'auth_profiles' || k.storage === 'openclaw_channels');
      // Per-field rendering: secret fields with active values get a Rotate button
      // (and Rollback when has_prev). Non-secret fields (e.g. Telegram chat_id)
      // get neither — they aren't rotation-worthy.
      const fieldRows = fields.map(f => {
        const fStatus = f.status === 'active' ? `<code style="font-size:0.77rem">${escHtml(f.masked||'—')}</code> ✅` : '<span style="color:var(--text3)">— ⚠</span>';
        let perFieldActions = '';
        if (f.secret && f.status === 'active' && rotatableStorage) {
          const lbl = `${k.provider} ${f.label}`;
          perFieldActions += ` <button class="btn btn-ghost btn-sm" style="padding:1px 6px;font-size:0.72rem"
            onclick="openRotateKeyModal(${q(botId)},${q(k.profile_id)},${q(k.provider)},${q(lbl)},null,${q(f.key)},null,${q(k.storage||'')})">↺ Rotate</button>`;
          if (f.has_prev) {
            perFieldActions += ` <button class="btn btn-ghost btn-sm" style="padding:1px 6px;font-size:0.72rem"
              onclick="submitRollbackKey(${q(botId)},${q(k.profile_id)},${q(k.provider)},${q(lbl)},${q(f.key)})">↩ Rollback</button>`;
          }
        }
        return `<span style="font-size:0.75rem;color:var(--text2)">${escHtml(f.label)}: </span>${fStatus}${perFieldActions}`;
      }).join(' &nbsp;|&nbsp; ');
      const addBtn = `<button class="btn btn-ghost btn-sm" onclick="openAddKeyModal(${q(k.provider)},${q(k.type)})">+ Configure</button>`;
      const removeBtn = anyActive ? `<button class="btn btn-ghost btn-sm" style="color:var(--red)" onclick="keysRemove(${q(botId)},${q(k.provider)},${q(k.profile_id)})">✕</button>` : '';
      return `<tr ${dp}${rowStyle}>
        <td><strong>${display}</strong></td>
        <td style="font-size:0.8rem;color:var(--text2)">${typeLabel}</td>
        <td colspan="2">${storageChip}${fieldRows}${pairInfo}${warningChip}</td>
        <td>—</td>
        <td style="text-align:right;white-space:nowrap">${anyActive ? removeBtn : addBtn}</td>
      </tr>`;
    }

    // api_key / identifier
    const optedOut = k.status === 'opted_out';
    const isPodInvariant = podInvariants.includes(k.provider);
    const maskedCell = (missing || optedOut)
      ? '<span style="color:var(--text3)">—</span>'
      : `<code style="font-size:0.78rem">${escHtml(k.masked || '—')}</code>`;
    // Live error state: check gateway errors for this provider
    const _botErrors = _gwCachedErrors(botId);
    const _provAlerts = (missing || optedOut) ? [] : parseApiErrors(_botErrors).filter(a => a.provider === k.provider);
    const _errBadge = _provAlerts.length
      ? ` ${renderErrorLabels(_provAlerts)}`
      : '';
    let statusBadge;
    if (optedOut) {
      const reason = k.opted_out_reason ? ` (${escHtml(k.opted_out_reason)})` : '';
      statusBadge = `<span style="color:var(--text3)">⊘ Opted out${reason}</span>`;
    } else if (missing) {
      statusBadge = isPodInvariant
        ? '<span style="color:var(--orange)">⚠ Setup required</span>'
        : '<span style="color:var(--text3)">⚠ Missing</span>';
    } else {
      statusBadge = `<span style="color:var(--green)">✅ Active</span>${_errBadge}`;
    }
    statusBadge = `${statusBadge}${warningChip}`;
    let orderCell = '<span style="color:var(--text3)">—</span>';
    if (k.order != null) {
      const suf = k.order === 1 ? 'st' : k.order === 2 ? 'nd' : 'rd';
      orderCell = `${k.order}${suf}`;
    }
    let actions;
    if (optedOut) {
      // Opt-out is a deliberate user choice — no Setup, no Rotate, no rollback.
      actions = '<span style="color:var(--text3);font-size:0.75rem">—</span>';
    } else if (missing) {
      actions = isPodInvariant
        ? `<button class="btn btn-ghost btn-sm" style="border:1px solid var(--orange);color:var(--orange)"
            onclick="openOnboardModal(${q(k.provider)},[${q(botId)}])">▶ Set up</button>`
        : `<button class="btn btn-ghost btn-sm"
            onclick="openAddKeyModal(${q(k.provider)},${q(k.type)})">+ Add</button>`;
    } else {
      const upBtn = (k.order != null && k.order > 1)
        ? `<button class="btn btn-ghost btn-sm" style="padding:2px 6px" title="Move up"
            onclick="keysReorder(${q(botId)},${q(k.provider)},${q(k.profile_id)},'up')">↑</button> `
        : '';
      const downBtn = (k.order != null && k.order_total != null && k.order < k.order_total)
        ? `<button class="btn btn-ghost btn-sm" style="padding:2px 6px" title="Move down"
            onclick="keysReorder(${q(botId)},${q(k.provider)},${q(k.profile_id)},'down')">↓</button> `
        : '';
      const rollbackBtn = k.has_prev
        ? `<button class="btn btn-ghost btn-sm" style="margin-right:4px" title="Roll back to previous key"
            onclick="submitRollbackKey(${q(botId)},${q(k.profile_id)},${q(k.provider)},${q(k.provider + ' ' + k.type_label)})">↩ Rollback</button>`
        : '';
      actions = upBtn + downBtn + rollbackBtn +
        `<button class="btn btn-ghost btn-sm" style="margin-right:4px"
          onclick="openRotateKeyModal(${q(botId)},${q(k.profile_id)},${q(k.provider)},${q(k.provider + ' ' + k.type_label)})">↺ Rotate</button>` +
        `<button class="btn btn-ghost btn-sm" style="color:var(--red)"
          onclick="keysRemove(${q(botId)},${q(k.provider)},${q(k.profile_id)})">✕</button>`;
    }
    return `<tr ${dp}${rowStyle}>
      <td>${display}</td>
      <td style="font-size:0.8rem;color:var(--text2)">${typeLabel}</td>
      <td>${maskedCell}</td>
      <td>${statusBadge}</td>
      <td style="font-size:0.8rem">${orderCell}</td>
      <td style="text-align:right;white-space:nowrap">${actions}</td>
    </tr>`;
  };

  // Phase 0 §4.3.d: opt the keys table into the resp-table primitive.
  // Wrapped per-category instance so data-label seeding (_respTableLabelize)
  // runs once at the end against every table under #ik-keys-table.
  const tableHeader = `<div class="resp-table-wrap"><table class="resp-table data-table"><thead><tr>
    <th>Provider</th><th>Type</th><th>Key (masked)</th><th>Status</th><th>Order</th><th style="text-align:right">Actions</th>
  </tr></thead><tbody>`;

  let html = sourceNote;
  for (const cat of CAT_ORDER) {
    const rows = byCategory[cat];
    if (!rows || !rows.length) continue;
    html += `<div style="font-size:0.7rem;font-weight:600;letter-spacing:0.08em;color:var(--text3);text-transform:uppercase;margin:16px 0 6px">${CAT_LABELS[cat] || cat}</div>`;
    html += tableHeader + rows.map(_keyRow).join('') + '</tbody></table></div>';
  }
  if (!Object.keys(byCategory).length) {
    html += '<div class="empty">No integrations configured.</div>';
  }
  el.innerHTML = html;
  _respTableLabelize(el);
  // Asynchronously scan the pod for pod-invariant gaps and render the bulk-onboard banner.
  // This is fire-and-forget; the keys table renders immediately regardless.
  _ikRenderOnboardBanner(podInvariants).catch(() => {});

  // Snapshot for evo's context-pack. Surface per-credential status so
  // evo can answer "which integrations have unhealthy keys?" / "are any
  // credentials about to expire?" / "which bot is missing the github
  // PAT?" without re-fetching. We bound the list to keep the payload
  // small but include enough per-row signal (provider + status +
  // warnings) for evo to point the operator at the right row.
  try {
    const projected = keys.map(k => ({
      provider: k.provider || null,
      type: k.type_label || k.type || null,
      status: k.status || null,
      class: k.credential_class || 'api_key',
      pod_invariant: podInvariants.includes(k.provider),
      warnings: Array.isArray(k.warnings) ? k.warnings.slice(0, 3) : [],
      has_warnings: Array.isArray(k.warnings) && k.warnings.length > 0,
    }));
    const missingInvariants = projected.filter(
      k => k.status === 'missing' && k.pod_invariant
    ).map(k => k.provider);
    const unhealthy = projected.filter(
      k => k.has_warnings || k.status === 'reauth_required' || k.status === 'expired'
    );
    _ikWriteContextSnapshot({
      bot: botId,
      credentials: {
        total: keys.length,
        active_count: projected.filter(k => k.status === 'active').length,
        missing_count: projected.filter(k => k.status === 'missing').length,
        unhealthy_count: unhealthy.length,
        missing_pod_invariants: missingInvariants,
        unhealthy_items: unhealthy.slice(0, 10),
        items: projected.slice(0, 24),
      },
    });
  } catch (_) {}
}

async function _ikRenderOnboardBanner(podInvariants) {
  const banner = document.getElementById('ik-onboard-banner');
  if (!banner) return;
  if (!Array.isArray(podInvariants) || !podInvariants.length) {
    banner.style.display = 'none';
    return;
  }
  const allBots = (typeof _networkData !== 'undefined' && _networkData && _networkData.bots)
    ? Object.keys(_networkData.bots) : [];
  if (!allBots.length) {
    banner.style.display = 'none';
    return;
  }
  // Per-provider count of bots missing.
  const missingByProvider = {};
  for (const p of podInvariants) missingByProvider[p] = [];
  for (const b of allBots) {
    try {
      const d = await api('GET', `/api/admin/keys/${b}`);
      for (const p of podInvariants) {
        const row = (d && d.keys || []).find(k => k.provider === p);
        if (row && row.status === 'missing') missingByProvider[p].push(b);
      }
    } catch (_) {}
  }
  const totalMissing = Object.values(missingByProvider).reduce((s, a) => s + a.length, 0);
  if (!totalMissing) {
    banner.style.display = 'none';
    banner.innerHTML = '';
    return;
  }
  const q = v => JSON.stringify(v).replace(/"/g, '&quot;');
  const buttons = podInvariants.map(p => {
    const bots = missingByProvider[p];
    if (!bots.length) return '';
    const label = p === 'github' ? 'GitHub' : p === 'brave' ? 'Brave' : p;
    return `<button class="btn btn-primary btn-sm" style="margin-right:6px"
      onclick='openOnboardModal(${q(p)},${JSON.stringify(bots).replace(/"/g, "&quot;")})'>
      ▶ Onboard ${escHtml(label)} (${bots.length} ${bots.length === 1 ? 'bot' : 'bots'})
    </button>`;
  }).filter(Boolean).join('');
  banner.innerHTML = `<div class="alert" style="background:rgba(255,165,0,0.08);border:1px solid var(--orange);padding:10px;border-radius:6px">
    <div style="font-size:0.82rem;margin-bottom:6px"><strong>Pod-wide setup gaps</strong> — ${totalMissing} bot${totalMissing === 1 ? '' : 's'} missing pod-invariant integrations.</div>
    ${buttons}
  </div>`;
  banner.style.display = 'block';
}

function openAddKeyModal(provider, keyType) {
  const sel = document.getElementById('add-key-provider');
  if (provider) sel.value = provider;
  else sel.value = '';
  const typeSel = document.getElementById('add-key-type');
  if (keyType) typeSel.value = keyType;
  else typeSel.value = 'api_key';
  _updateAddKeyTypeVisibility();
  document.getElementById('add-key-value').value = '';
  document.getElementById('add-key-result').innerHTML = '';
  // Reset source toggle to "Paste" on each open.
  const pasteRadio = document.querySelector('input[name="add-key-source"][value="paste"]');
  if (pasteRadio) pasteRadio.checked = true;
  _updateAddKeySourceMode();
  // Populate the borrow dropdown for the pre-filled provider (if any).
  _updateAddKeyBorrowOptions();
  document.getElementById('add-key-modal').classList.add('open');
}

function _updateAddKeyTypeVisibility() {
  const provider = document.getElementById('add-key-provider').value;
  const typeRow = document.getElementById('add-key-type-row');
  // Only Anthropic has multiple credential types
  typeRow.style.display = provider === 'anthropic' ? '' : 'none';
}

function _updateAddKeySourceMode() {
  const mode = (document.querySelector('input[name="add-key-source"]:checked') || {}).value || 'paste';
  document.getElementById('add-key-paste-row').style.display = mode === 'paste' ? '' : 'none';
  document.getElementById('add-key-borrow-row').style.display = mode === 'borrow' ? '' : 'none';
}

// Populate the "Copy from" dropdown with bots that have a credentialed
// profile for the currently-selected provider. Hides the borrow option
// entirely (greying out the radio + hint text) when no other bot in
// the pod has the provider configured.
async function _updateAddKeyBorrowOptions() {
  const provider = document.getElementById('add-key-provider').value;
  const borrowSel = document.getElementById('add-key-borrow-source');
  const borrowLabel = document.getElementById('add-key-source-borrow-label');
  const borrowRadio = borrowLabel ? borrowLabel.querySelector('input[type=radio]') : null;
  const hint = document.getElementById('add-key-borrow-hint');
  if (!borrowSel || !borrowLabel || !hint) return;

  borrowSel.innerHTML = '<option value="">Select a bot…</option>';
  if (!provider) {
    borrowLabel.style.opacity = '0.5';
    if (borrowRadio) borrowRadio.disabled = true;
    hint.style.display = 'none';
    return;
  }

  // The exclude param keeps the target bot itself out of the list —
  // copy-from-self is a no-op.
  const exclude = _ikBot || '';
  const url = `/api/admin/keys/borrow-candidates?provider=${encodeURIComponent(provider)}&exclude=${encodeURIComponent(exclude)}`;
  const r = await api('GET', url);
  const cands = (r && Array.isArray(r.candidates)) ? r.candidates : [];

  if (cands.length === 0) {
    borrowLabel.style.opacity = '0.5';
    if (borrowRadio) borrowRadio.disabled = true;
    hint.style.display = '';
    hint.textContent = `No other bot has a ${provider} credential to copy.`;
    // If "borrow" was selected, switch back to paste.
    if (borrowRadio && borrowRadio.checked) {
      const pasteRadio = document.querySelector('input[name="add-key-source"][value="paste"]');
      if (pasteRadio) pasteRadio.checked = true;
      _updateAddKeySourceMode();
    }
    return;
  }

  borrowLabel.style.opacity = '';
  if (borrowRadio) borrowRadio.disabled = false;
  hint.style.display = 'none';
  for (const c of cands) {
    const opt = document.createElement('option');
    opt.value = c.bot_id;
    opt.textContent = c.bot_id;
    borrowSel.appendChild(opt);
  }
}

function closeAddKeyModal() {
  document.getElementById('add-key-modal').classList.remove('open');
}

async function submitAddKey() {
  const provider = document.getElementById('add-key-provider').value;
  const keyType = document.getElementById('add-key-type').value || 'api_key';
  const resultEl = document.getElementById('add-key-result');
  const mode = (document.querySelector('input[name="add-key-source"]:checked') || {}).value || 'paste';
  if (!provider) { resultEl.innerHTML = '<div class="alert alert-error">Select a provider.</div>'; return; }
  if (!_ikBot) { resultEl.innerHTML = '<div class="alert alert-error">No bot selected.</div>'; return; }

  if (mode === 'borrow') {
    const fromBot = document.getElementById('add-key-borrow-source').value;
    if (!fromBot) { resultEl.innerHTML = '<div class="alert alert-error">Choose a bot to copy from.</div>'; return; }
    resultEl.innerHTML = '<div class="loading"><span class="spinner"></span> Copying…</div>';
    const r = await api('POST', `/api/admin/keys/${_ikBot}/${provider}/borrow`, {
      from_bot: fromBot, key_type: keyType,
    });
    if (r.error) {
      resultEl.innerHTML = `<div class="alert alert-error">${escHtml(r.error)}</div>`;
      return;
    }
    resultEl.innerHTML = `<div class="alert alert-ok">Copied ${escHtml(provider)} from ${escHtml(fromBot)}.</div>`;
    setTimeout(closeAddKeyModal, 1200);
    loadKeys();
    return;
  }

  const keyValue = document.getElementById('add-key-value').value.trim();
  if (!keyValue) { resultEl.innerHTML = '<div class="alert alert-error">Enter the key value.</div>'; return; }
  resultEl.innerHTML = '<div class="loading"><span class="spinner"></span> Saving…</div>';
  const r = await api('POST', `/api/admin/keys/${_ikBot}/${provider}`, { key_value: keyValue, key_type: keyType });
  if (r.error) {
    resultEl.innerHTML = `<div class="alert alert-error">${escHtml(r.error)}</div>`;
  } else {
    resultEl.innerHTML = '<div class="alert alert-ok">Key saved.</div>';
    setTimeout(closeAddKeyModal, 1200);
    loadKeys();
  }
}

async function keysSync() { await ikRefreshKeys(); } // compat alias

let _rotateKeyState = {};

function openRotateKeyModal(botId, profileId, provider, label, endpointOverride, fieldKey, noticeKind, storage) {
  _rotateKeyState = {
    botId, profileId, provider,
    endpoint: endpointOverride || null,
    fieldKey: fieldKey || null,
    // "auth_profiles" (default) vs "openclaw_channels" — telegram/slack
    // bots configured outside the wizard need the latter so the rotate
    // endpoint writes to channels.<provider>.<oc_field> directly.
    storage: storage || null,
  };
  document.getElementById('rotate-key-label').textContent = label;
  document.getElementById('rotate-key-value').value = '';
  document.getElementById('rotate-key-result').innerHTML = '';
  // Optional notice — used today for github credhelper bots so the operator
  // knows clicking Rotate switches the bot's auth model.
  const noticeEl = document.getElementById('rotate-key-notice');
  if (noticeEl) {
    if (noticeKind === 'credhelper_to_pat') {
      noticeEl.innerHTML = `<strong>Heads up:</strong> ${escHtml(botLabel(botId))} currently authenticates via credential helper (the URL has no embedded token). Pasting a PAT here will rewrite <code>.git/config</code> to <code>https://&lt;PAT&gt;@github.com/&hellip;</code> so future rotations can edit it in-place.`;
      noticeEl.style.display = '';
    } else {
      noticeEl.innerHTML = '';
      noticeEl.style.display = 'none';
    }
  }
  document.getElementById('rotate-key-modal').classList.add('open');
}

function closeRotateKeyModal() {
  document.getElementById('rotate-key-modal').classList.remove('open');
}

async function submitRotateKey() {
  const keyValue = document.getElementById('rotate-key-value').value.trim();
  const resultEl = document.getElementById('rotate-key-result');
  if (!keyValue) { resultEl.innerHTML = '<div class="alert alert-error">Enter the new key value.</div>'; return; }
  const { botId, profileId, provider: storedProvider, endpoint, fieldKey, storage } = _rotateKeyState;
  const provider = storedProvider || (profileId || '').split(/[_:]/)[0] || profileId;
  const url = endpoint || `/api/admin/keys/${botId}/${provider}/rotate`;
  resultEl.innerHTML = '<div class="loading"><span class="spinner"></span> Saving…</div>';
  const payload = { key_value: keyValue, profile_id: profileId };
  if (fieldKey) payload.field_key = fieldKey;
  if (storage) payload.storage = storage;
  const r = await api('POST', url, payload);
  if (r.error) {
    resultEl.innerHTML = `<div class="alert alert-error">${escHtml(r.error)}</div>`;
    return;
  }
  const gitNote = r.git_config_updated === false ? ' (git config not updated — check .git/config manually)' : '';
  const mirrorNote = r.mirrored === false ? ` (openclaw.json mirror failed: ${escHtml(r.mirror_error || '?')})` : '';
  resultEl.innerHTML = `<div class="alert alert-ok">Token rotated.${gitNote}${mirrorNote}</div>`;
  setTimeout(closeRotateKeyModal, 1400);
  loadKeys();
  if (r.requires_restart && r.restart_endpoint) {
    setTimeout(() => _promptGatewayRestart(botId, r.restart_endpoint), 1500);
  }
}

async function _promptGatewayRestart(botId, restartEndpoint) {
  if (!await confirmModal(`Restart ${botLabel(botId)}'s gateway now to apply the rotated credential?`)) return;
  const r = await api('POST', restartEndpoint, { confirm: true });
  if (r && r.ok) {
    toast(`${botLabel(botId)} gateway restarted.`, 'ok');
  } else {
    toast(`Gateway restart failed: ${(r && r.error) || 'unknown error'}`, 'error');
  }
}

// ── WhatsApp setup / enable / disable ──────────────────────────────────
// WhatsApp has no rotatable token (Baileys session-based auth). Setup writes
// the channel scaffold and flips channels.whatsapp.enabled in openclaw.json,
// then surfaces the next-steps text from the server (which describes the CLI
// pairing flow that runs on the bot host). No silent no-op: the server reports

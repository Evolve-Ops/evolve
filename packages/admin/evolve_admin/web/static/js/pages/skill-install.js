// ════════════════════════════════════════════════════════════════════════
// Page subtab: Skill install flow + Google Workspace OAuth wizards
//
// Four contiguous sub-clusters covering the per-bot "Add Gmail/Calendar/
// other GOG skill" install pipeline:
//
//   1. Skill install flow (Spec 11) — openSkillInstall + the wizard
//      (intro, status line, progress bar, form renderer, capability
//      picker, submit, drive-plan handler, poll-until-active, result
//      renderer, GWS-complete success/failure handlers, close +
//      cleanup).
//
//   2. Shared awaiting_oauth install-plan renderer (V2.1-4) —
//      _renderAwaitingOauthInstallPlan: rendered by both the install
//      flow above and the per-bot Plugins page when an OAuth handshake
//      is mid-flight.
//
//   3. Google Workspace OAuth wizard — services screen + current-row
//      load + scope preview + poll loop + result renderer + share-link
//      / copy-to-clipboard + the per-bot configure flow.
//
//   4. Path-C Google Workspace wizard (PR ε) — Path-C entrypoint
//      (operator-provided OAuth secret) + scope picker + configure +
//      copy-to-clipboard share helper.
//
//   5. Unified Google chooser — single entry point that branches into
//      Path A (default), Path B (BYOSecret), or Path C (full Workspace)
//      depending on what the operator wants.
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - api(), toast(), escHtml(), botLabel() — core/
//   - skillsLoadPod / _skillsRenderPod — pages/skills.js (Phase 3r;
//     refreshes the Skills page after a successful install)
//   - loadIntegrationsKeys — still inline in main script (Plugins page
//     reloader called when OAuth completes)
// ════════════════════════════════════════════════════════════════════════


// ══════════════════════════════════════════════════════
// Skill install flow (Spec 11 — GOG / Gmail+Calendar)
// ══════════════════════════════════════════════════════
//
// Friendly per-bot "Add Gmail/Calendar" entry point. The flow:
//   1. GET /api/skills/<skill_id>           → access panel copy (Will/Won't)
//   2. GET /api/skills/install/<id>/status  → current state
//   3. POST .../install/<id>                → ordered install plan
//   4. POST .../install/<id>/enable-plugin  → ensure plugin entry is on
//   5. openGoogleWorkspaceWizard()          → existing OAuth flow
//   6. Poll status until "active" or error
//
// The plain-language access panel is the trust UX promise; copy is rendered
// verbatim from the server-side GOG_ACCESS_PANEL, never hand-written in this
// file (Plex test — no scopes/jargon visible to user).

const _skillInstallState = {
  botId: null,
  skillId: null,
  plan: null,
  pollTimer: null,
  // Slack OAuth (V2.1-2 endpoint contract): popup window, postMessage
  // listener, and the state token tying a tab back to a /poll response.
  slackPopupRef: null,
  slackMsgListener: null,
  slackOAuthState: null,
  // Credential-paste form state (Home Assistant LLAT etc.). Set by
  // _skillInstallDrivePlan when it hits a set_config step; consumed by
  // skillInstallSubmitForm to drive the rest of the plan after submission.
  pendingFormStep: null,
  formRemainingSteps: null,
  // Unified Google skill — capability picker state. Set by the
  // pick_capabilities step in _skillInstallDrivePlan; consumed by
  // _skillInstallSubmitCapabilityPicker.
  pendingPickerStep: null,
  pickerRemainingSteps: null,
  pickedCapabilities: null,
};

async function openSkillInstall(skillId, botId) {
  if (!skillId || !botId) {
    toast('skill id and bot id required', 'err');
    return;
  }
  _skillInstallState.skillId = skillId;
  _skillInstallState.botId = botId;
  _skillInstallState.plan = null;
  _skillInstallCleanup();

  // Reset all four screens; show the modal.
  document.getElementById('skill-install-screen-intro').style.display = 'none';
  document.getElementById('skill-install-screen-progress').style.display = 'none';
  document.getElementById('skill-install-screen-result').style.display = 'none';
  document.getElementById('skill-install-screen-form').style.display = 'none';
  document.getElementById('skill-install-progress-error').textContent = '';
  document.getElementById('skill-install-status-line').textContent = '';
  document.getElementById('skill-install-modal').classList.add('open');
  _skillInstallState.pendingFormStep = null;
  _skillInstallState.formRemainingSteps = null;
  _skillInstallState.pendingPickerStep = null;
  _skillInstallState.pickerRemainingSteps = null;
  _skillInstallState.pickedCapabilities = null;

  // Fetch the skill metadata (access panel copy). We render before plan so
  // the user sees the "Will / Won't" panel even if the status fetch hangs.
  let meta;
  try {
    meta = await api('GET', '/api/skills/catalog/' + encodeURIComponent(skillId));
  } catch (e) {
    _skillInstallShowResult(false, 'Could not load skill metadata',
      (e && e.message) || String(e));
    return;
  }
  if (!meta || meta.error) {
    _skillInstallShowResult(false, 'Could not load skill metadata',
      (meta && meta.error) || 'unknown error');
    return;
  }
  _skillInstallState.displayName = (meta && meta.display_name) || skillId;
  _skillInstallRenderIntro(meta, botId);

  // Fetch current install status in the background; reflect it in the
  // status line so the user knows whether anything's already in place.
  try {
    const st = await api(
      'GET',
      '/api/skills/install/' + encodeURIComponent(skillId)
        + '/status?bot_id=' + encodeURIComponent(botId),
    );
    if (st && st.ok) {
      _skillInstallSetStatusLine(st);
      // 'active' = GOG completion state; 'valid' = Slack (V2.1-2).
      // The unified ``google`` skill (post-PR #2154) is an exception:
      // its plan for active state returns the capability-picker chain
      // (so the user can MODIFY what the bot is allowed to do). We let
      // the plan drive instead of showing the dead-end "Already
      // installed" screen.
      if ((st.status === 'active' || st.status === 'valid') && skillId !== 'google') {
        const accountSuffix = st.google_account ? ` as ${st.google_account}`
          : st.workspace_name ? ` (${st.workspace_name})`
          : '';
        _skillInstallShowResult(true,
          'Already installed',
          `This bot is already connected to ${meta.display_name}${accountSuffix}.`);
      }
    }
  } catch (_) { /* non-fatal — Continue button still works */ }
}

function _skillInstallRenderIntro(meta, botId) {
  document.getElementById('skill-install-title').textContent =
    `Add ${meta.display_name} to ${botLabel(botId)}`;
  document.getElementById('skill-install-subtitle').textContent =
    `Connect this bot to ${meta.display_name || _skillInstallState.skillId}.`;
  document.getElementById('skill-install-summary').textContent =
    (meta.access_panel && meta.access_panel.summary) || meta.summary || '';

  const willEl = document.getElementById('skill-install-will');
  const wontEl = document.getElementById('skill-install-wont');
  willEl.innerHTML = '';
  wontEl.innerHTML = '';
  const ap = meta.access_panel || {};
  (ap.will || []).forEach(line => {
    const li = document.createElement('li');
    li.textContent = line;
    willEl.appendChild(li);
  });
  (ap.wont || []).forEach(line => {
    const li = document.createElement('li');
    li.textContent = line;
    wontEl.appendChild(li);
  });

  document.getElementById('skill-install-credentials-note').textContent =
    ap.where_credentials_live ||
    'Tokens are stored only on this bot, never sent off-pod.';

  document.getElementById('skill-install-screen-intro').style.display = 'block';
}

function _skillInstallSetStatusLine(st) {
  const el = document.getElementById('skill-install-status-line');
  if (!el || !st) return;
  switch (st.status) {
    case 'active': {
      // Per-skill account suffix: same fields the 'valid' case checks
      // (workspace_name for Slack, bot_username/bot_first_name for Telegram,
      // google_account for GWS). Use the first one present.
      const suffix = st.workspace_name ? ` to ${st.workspace_name}`
                   : st.bot_username    ? ` as @${st.bot_username}`
                   : st.bot_first_name  ? ` as ${st.bot_first_name}`
                   : st.google_account  ? ` as ${st.google_account}`
                   : '';
      el.textContent = `Already connected${suffix}.`;
      el.style.color = 'var(--green, #34a853)';
      break;
    }
    case 'oauth_pending': {
      const name = (_skillInstallState && _skillInstallState.displayName)
                 || _skillInstallState?.skillId
                 || 'this service';
      el.textContent = `Plugin is on; just needs ${name} sign-in.`;
      el.style.color = 'var(--text2)';
      break;
    }
    case 'plugin_disabled': {
      const name = (_skillInstallState && _skillInstallState.displayName)
                 || _skillInstallState?.skillId
                 || 'this service';
      el.textContent = `The ${name} plugin will be turned on first, then you will sign in.`;
      el.style.color = 'var(--text2)';
      break;
    }
    case 'oauth_client_missing':
      el.textContent =
        'One-time setup needed: a Google Cloud OAuth client. The Google Workspace wizard will handle it.';
      el.style.color = 'var(--orange, #fbbc04)';
      break;
    // ── Token/credential skill states (Slack V2.1-2, Telegram, etc.) ────
    case 'valid': {
      // Per-skill account suffix: workspace_name (Slack), bot_username
      // (Telegram), google_account (GWS), or empty for skills with no
      // identifying remote handle.
      const suffix = st.workspace_name ? ` to ${st.workspace_name}`
                   : st.bot_username    ? ` as @${st.bot_username}`
                   : st.bot_first_name  ? ` as ${st.bot_first_name}`
                   : '';
      el.textContent = `Already connected${suffix}.`;
      el.style.color = 'var(--green, #34a853)';
      break;
    }
    case 'missing': {
      const name = (_skillInstallState && _skillInstallState.displayName)
                 || _skillInstallState?.skillId
                 || 'this service';
      el.textContent = `This bot has no ${name} connection yet.`;
      el.style.color = 'var(--text2)';
      break;
    }
    case 'revoked': {
      const name = (_skillInstallState && _skillInstallState.displayName)
                 || _skillInstallState?.skillId
                 || 'previous';
      el.textContent = `The previous ${name} token was revoked. Re-authorize to reconnect.`;
      el.style.color = 'var(--orange, #fbbc04)';
      break;
    }
    case 'credentials_missing':
      el.textContent =
        'One-time setup needed: pod admin must register a Slack app. See docs/skills/slack-setup.md.';
      el.style.color = 'var(--orange, #fbbc04)';
      break;
    default:
      el.textContent = '';
  }
}

async function skillInstallContinue() {
  const skillId = _skillInstallState.skillId;
  const botId = _skillInstallState.botId;
  if (!skillId || !botId) return;

  // POST /api/skills/install/<id> → get the ordered plan (or orchestrator response)
  let resp;
  try {
    resp = await api('POST', '/api/skills/install/' + encodeURIComponent(skillId),
      { bot_id: botId });
  } catch (e) {
    _skillInstallShowResult(false, 'Could not start install',
      (e && e.message) || String(e));
    return;
  }
  if (!resp || !resp.ok) {
    _skillInstallShowResult(false, 'Could not start install',
      (resp && resp.error) || 'unknown error');
    return;
  }

  // V2.1-2 Slack: drive the popup + poll flow directly. We deliberately do
  // this *after* the plan POST (so audit logs + already-active detection
  // still run server-side) but before the generic step-plan / awaiting_oauth
  // dispatch — Slack's completion state is 'valid', not 'active', and the
  // OAuth round-trip is mediated by a state-token + popup rather than the
  // orchestrator's action_url. Once V2.1-2 lands and is normalized against
  // the orchestrator contract, this branch can fold back into the standard
  // awaiting_oauth path.
  if (skillId === 'slack') {
    if (resp.status === 'already_active' ||
        (resp.status && resp.status.token_state === 'valid')) {
      const ws = (resp.status && resp.status.workspace_name) || '';
      _skillInstallShowResult(true,
        'Already installed',
        ws ? `This bot is already connected to Slack (${ws}).`
           : 'This bot is already connected to Slack.');
      return;
    }
    document.getElementById('skill-install-screen-intro').style.display = 'none';
    document.getElementById('skill-install-screen-progress').style.display = 'block';
    // Best-effort step list for the user while OAuth is in flight.
    _skillInstallRenderProgress(
      (resp.steps && resp.steps.length)
        ? resp.steps
        : [{ id: 'oauth', label: 'Connect to your Slack workspace' },
           { id: 'confirm', label: 'Confirm the bot can send messages to Slack' }],
      0,
    );
    await _slackInstallRunOAuth();
    return;
  }

  // V2.1-4: orchestrator short-circuit paths for OAuth skills
  if (resp.status === 'already_active') {
    _skillInstallShowResult(true, 'Already installed', resp.message || 'This bot is already set up.');
    return;
  }

  if (resp.status === 'awaiting_oauth') {
    // Show the unified awaiting_oauth panel in the progress screen.
    document.getElementById('skill-install-screen-intro').style.display = 'none';
    document.getElementById('skill-install-screen-progress').style.display = 'block';
    _renderAwaitingOauthInstallPlan(
      resp.missing || [],
      /* statusEndpoint */ '/api/skills/install/' + encodeURIComponent(skillId) + '/status?bot_id=' + encodeURIComponent(botId),
      /* onResume */ () => {
        _skillInstallShowResult(true, 'All set', 'This bot can now use ' + skillId + '.');
      },
      /* onTimeout */ () => {
        const name = (_skillInstallState && _skillInstallState.displayName) || skillId;
        _skillInstallShowResult(false, 'Still working on it',
          `${name} setup is not complete yet. You can close this and re-open to check status.`);
      },
      /* renderTarget */ document.getElementById('skill-install-step-list'),
      /* botId       */ botId,
    );
    return;
  }

  _skillInstallState.plan = resp;

  // Move to the progress screen.
  document.getElementById('skill-install-screen-intro').style.display = 'none';
  document.getElementById('skill-install-screen-progress').style.display = 'block';
  _skillInstallRenderProgress(resp.steps, 0);

  // Drive the plan: enable-plugin first if present, then OAuth.
  await _skillInstallDrivePlan(resp);
}

function _skillInstallRenderProgress(steps, currentIdx) {
  const el = document.getElementById('skill-install-step-list');
  el.innerHTML = '';
  if (!Array.isArray(steps) || !steps.length) {
    el.textContent = 'Nothing to do — this bot is already set up.';
    return;
  }
  steps.forEach((s, i) => {
    const row = document.createElement('div');
    row.style.padding = '8px 0';
    row.style.borderBottom = '1px solid var(--border)';
    const status = i < currentIdx ? 'done'
      : i === currentIdx ? 'doing'
      : 'pending';
    const icon = status === 'done' ? '✓'
      : status === 'doing' ? '…'
      : '○';
    const color = status === 'done' ? 'var(--green, #34a853)'
      : status === 'doing' ? 'var(--accent)'
      : 'var(--text3)';
    row.innerHTML =
      `<span style="display:inline-block;width:18px;color:${color};font-weight:700">${icon}</span>` +
      `<span style="color:${status==='pending'?'var(--text3)':'var(--text1)'}">${escapeHtml(s.label)}</span>`;
    el.appendChild(row);
  });
}

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, ch =>
    ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[ch]));
}

// ── Credential-form step renderer (set_config) ──────────────────────────────
// Drives the 4th install screen: a dynamic form generated from step.fields[].
// Used today by Home Assistant (URL + LLAT); shape is generic so the next
// paste-style skill (Notion internal-integration token, etc.) needs no JS.
function _skillInstallRenderForm(step) {
  document.getElementById('skill-install-screen-intro').style.display    = 'none';
  document.getElementById('skill-install-screen-progress').style.display = 'none';
  document.getElementById('skill-install-screen-result').style.display   = 'none';
  document.getElementById('skill-install-screen-form').style.display     = 'block';

  document.getElementById('skill-install-form-title').textContent = step.label || '';
  document.getElementById('skill-install-form-error').textContent = '';

  const submitBtn = document.getElementById('skill-install-form-submit-btn');
  submitBtn.disabled = false;
  submitBtn.textContent = 'Save and connect';

  const wrap = document.getElementById('skill-install-form-fields');
  wrap.innerHTML = '';
  const fields = Array.isArray(step.fields) ? step.fields : [];
  fields.forEach(f => {
    const row = document.createElement('div');
    row.style.display = 'flex';
    row.style.flexDirection = 'column';
    row.style.gap = '4px';

    const label = document.createElement('label');
    label.textContent = f.label || f.name;
    label.style.fontSize = '0.78rem';
    label.style.color = 'var(--text2)';
    label.style.fontWeight = '600';
    row.appendChild(label);

    const input = document.createElement('input');
    input.type = f.type === 'password' ? 'password'
               : f.type === 'url'      ? 'url'
               : 'text';
    input.id = 'skill-install-form-field-' + f.name;
    input.placeholder = f.placeholder || '';
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.style.padding = '8px 10px';
    input.style.background = 'var(--bg2)';
    input.style.border = '1px solid var(--border)';
    input.style.borderRadius = '6px';
    input.style.color = 'var(--text1)';
    input.style.fontFamily = 'inherit';
    input.style.boxSizing = 'border-box';
    input.style.width = '100%';
    row.appendChild(input);

    if (f.help) {
      const help = document.createElement('div');
      help.textContent = f.help;
      help.style.fontSize = '0.72rem';
      help.style.color = 'var(--text3)';
      help.style.lineHeight = '1.4';
      row.appendChild(help);
    }
    wrap.appendChild(row);
  });

  // Focus the first field for keyboard-driven flow.
  const firstField = fields[0];
  if (firstField) {
    const el = document.getElementById('skill-install-form-field-' + firstField.name);
    if (el) setTimeout(() => el.focus(), 30);
  }
}

// ── Capability picker (unified Google skill, post-PR #2154 review) ─────────
//
// Renders into the existing form-screen DOM (reuses the title + error +
// submit button). Each capability appears as a checkbox grouped by app
// (Gmail / Calendar / Drive / Sheets / Docs). The Advanced section is a
// collapsed disclosure for restricted scopes (gmail.modify / drive).
//
// On submit, the picked capability ids land in
// _skillInstallState.pickedCapabilities and the wizard advances by
// rewriting the remaining oauth + complete steps' payloads with the
// pick. No backend POST for this step — it's UI-only.

const _CAPABILITY_GROUP_ORDER = ['Gmail', 'Calendar', 'Drive', 'Sheets', 'Docs', 'Advanced'];

function _skillInstallRenderCapabilityPicker(step) {
  document.getElementById('skill-install-screen-intro').style.display    = 'none';
  document.getElementById('skill-install-screen-progress').style.display = 'none';
  document.getElementById('skill-install-screen-result').style.display   = 'none';
  document.getElementById('skill-install-screen-form').style.display     = 'block';

  document.getElementById('skill-install-form-title').textContent = step.label || 'Pick capabilities';
  document.getElementById('skill-install-form-error').textContent = '';

  const submitBtn = document.getElementById('skill-install-form-submit-btn');
  submitBtn.disabled = false;
  submitBtn.textContent = 'Sign in with Google';

  const wrap = document.getElementById('skill-install-form-fields');
  wrap.innerHTML = '';

  // Group capabilities by their `group` field.
  const byGroup = {};
  for (const cap of (step.capabilities || [])) {
    (byGroup[cap.group] = byGroup[cap.group] || []).push(cap);
  }

  for (const group of _CAPABILITY_GROUP_ORDER) {
    const caps = byGroup[group];
    if (!caps || !caps.length) continue;
    const isAdvanced = group === 'Advanced';

    // Section header
    const head = document.createElement('div');
    if (isAdvanced) {
      head.innerHTML = `<span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span> Advanced (sensitive scopes)`;
      head.style.display = 'inline-flex';
      head.style.alignItems = 'center';
      head.style.gap = '6px';
    } else {
      head.textContent = group;
    }
    head.style.fontWeight = '600';
    head.style.fontSize = isAdvanced ? '0.78rem' : '0.85rem';
    head.style.color = isAdvanced ? 'var(--text3)' : 'var(--text1)';
    head.style.marginTop = '12px';
    head.style.marginBottom = '4px';
    if (isAdvanced) head.style.cursor = 'pointer';
    wrap.appendChild(head);

    // Per-group capability container
    const container = document.createElement('div');
    container.style.display = isAdvanced ? 'none' : 'flex';
    container.style.flexDirection = 'column';
    container.style.gap = '6px';
    container.style.marginLeft = '6px';

    if (isAdvanced) {
      head.onclick = () => {
        const open = container.style.display !== 'none';
        container.style.display = open ? 'none' : 'flex';
        const ico = head.querySelector('.expand-icon');
        if (ico) ico.classList.toggle('is-open', !open);
      };
    }

    for (const cap of caps) {
      const row = document.createElement('label');
      row.style.display = 'flex';
      row.style.alignItems = 'flex-start';
      row.style.gap = '8px';
      row.style.fontSize = '0.85rem';
      row.style.cursor = 'pointer';
      row.style.padding = '3px 0';
      row.style.color = 'var(--text1)';

      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.id = 'skill-install-cap-' + cap.id;
      cb.checked = !!cap.checked;
      cb.dataset.capabilityId = cap.id;
      cb.style.marginTop = '2px';
      // Wire change → recompute the inline picker warnings (restricted-
      // scope banner + drive-subset hint).
      cb.addEventListener('change', _skillInstallUpdatePickerWarnings);
      row.appendChild(cb);

      const text = document.createElement('div');
      text.style.flex = '1';
      const labelLine = document.createElement('div');
      labelLine.textContent = cap.label;
      text.appendChild(labelLine);
      if (cap.restricted) {
        const tag = document.createElement('div');
        tag.textContent = 'Google verification required for personal Gmail accounts.';
        tag.style.fontSize = '0.72rem';
        tag.style.color = 'var(--text3)';
        tag.style.marginTop = '2px';
        text.appendChild(tag);
      }
      // Per-capability inline hint slot (currently only used for
      // drive_write to surface the drive.file subset constraint).
      // Pre-built hidden; toggled by _skillInstallUpdatePickerWarnings.
      if (cap.id === 'drive_write') {
        const hint = document.createElement('div');
        hint.id = 'skill-install-cap-drive_write-hint';
        hint.textContent = (
          "This grants narrow write access — the bot can only edit " +
          "Drive files it created or that you've explicitly shared " +
          "with it. To let the bot find and edit any of your Drive " +
          "files, also check 'Read your Google Drive files' above."
        );
        hint.style.display = 'none';
        hint.style.fontSize = '0.72rem';
        hint.style.color = 'var(--orange, #c8830a)';
        hint.style.background = 'var(--bg2)';
        hint.style.border = '1px solid var(--orange, #c8830a)';
        hint.style.borderRadius = '4px';
        hint.style.padding = '6px 8px';
        hint.style.marginTop = '4px';
        hint.style.lineHeight = '1.4';
        text.appendChild(hint);
      }
      row.appendChild(text);

      container.appendChild(row);
    }
    wrap.appendChild(container);
  }

  // Restricted-scope confirmation banner — shown when ANY capability in
  // the Advanced group is checked. Pre-built hidden; toggled by
  // _skillInstallUpdatePickerWarnings.
  const restrictedBanner = document.createElement('div');
  restrictedBanner.id = 'skill-install-picker-restricted-banner';
  restrictedBanner.style.display = 'none';
  restrictedBanner.style.marginTop = '14px';
  restrictedBanner.style.padding = '10px 12px';
  restrictedBanner.style.background = 'var(--bg2)';
  restrictedBanner.style.border = '1px solid var(--yellow, #d4a72c)';
  restrictedBanner.style.borderRadius = '6px';
  restrictedBanner.style.fontSize = '0.78rem';
  restrictedBanner.style.lineHeight = '1.5';
  restrictedBanner.style.color = 'var(--text1)';
  restrictedBanner.innerHTML = (
    '<strong style="color:var(--yellow, #d4a72c)">⚠ Google verification required.</strong> ' +
    "You picked a capability that Google considers sensitive. " +
    "On a personal Gmail account, the OAuth consent screen will show an " +
    "<strong>'unverified app' warning</strong> for these scopes. To grant them, " +
    "click <strong>Advanced</strong> on the warning screen, then " +
    "<strong>'Go to Evolve (unsafe)'</strong>. This is your own machine — " +
    "no risk. Workspace accounts can skip the warning by approving the app " +
    "domain-wide in Workspace Admin."
  );
  wrap.appendChild(restrictedBanner);

  // Footer hint
  const hint = document.createElement('div');
  hint.textContent = 'Pick what you want this bot to do. You can change this later by re-running the wizard.';
  hint.style.fontSize = '0.72rem';
  hint.style.color = 'var(--text3)';
  hint.style.marginTop = '16px';
  hint.style.lineHeight = '1.5';
  wrap.appendChild(hint);

  // Initial warning state — call after the DOM has been built.
  _skillInstallUpdatePickerWarnings();
}

// Recompute the picker's two inline warnings based on current checkbox
// state. Called once after initial render + on every checkbox change.
//
//   * Restricted-scope banner: any capability in the Advanced group
//     is checked → show the banner explaining the unverified-app
//     warning Google will display on the consent screen.
//   * Drive subset hint: drive_write checked but drive_read not →
//     surface that drive.file is narrow (bot-created / shared files
//     only) and recommend adding drive_read for broader access.
//
// Safe to call when the picker isn't currently rendered — the
// getElementById lookups return null and the function exits cleanly.
function _skillInstallUpdatePickerWarnings() {
  const step = _skillInstallState.pendingPickerStep;
  if (!step || !Array.isArray(step.capabilities)) return;

  // Tally checked + restricted state
  let anyRestrictedChecked = false;
  let driveWriteChecked = false;
  let driveReadChecked = false;
  for (const cap of step.capabilities) {
    const el = document.getElementById('skill-install-cap-' + cap.id);
    if (!el || !el.checked) continue;
    if (cap.restricted) anyRestrictedChecked = true;
    if (cap.id === 'drive_write') driveWriteChecked = true;
    if (cap.id === 'drive_read')  driveReadChecked = true;
  }

  const banner = document.getElementById('skill-install-picker-restricted-banner');
  if (banner) banner.style.display = anyRestrictedChecked ? 'block' : 'none';

  const driveHint = document.getElementById('skill-install-cap-drive_write-hint');
  if (driveHint) {
    driveHint.style.display = (driveWriteChecked && !driveReadChecked) ? 'block' : 'none';
  }
}

async function _skillInstallSubmitCapabilityPicker() {
  const step = _skillInstallState.pendingPickerStep;
  if (!step) return;
  const errEl = document.getElementById('skill-install-form-error');
  const btn   = document.getElementById('skill-install-form-submit-btn');
  errEl.textContent = '';

  // Collect checked capability ids from the DOM.
  const picked = [];
  for (const cap of (step.capabilities || [])) {
    const el = document.getElementById('skill-install-cap-' + cap.id);
    if (el && el.checked) picked.push(cap.id);
  }
  if (picked.length === 0) {
    errEl.textContent = 'Pick at least one capability.';
    return;
  }

  _skillInstallState.pickedCapabilities = picked;
  _skillInstallState.pendingPickerStep = null;

  // Modify-flow optimisation (spec §13.1 Q3): when the operator is
  // re-running the wizard on an already-installed bot AND every
  // picked capability is already granted (i.e. picked ⊆ currently_granted),
  // SKIP the oauth step. Google's consent screen would skip silently
  // anyway, but the popup-then-close UX is mildly disorienting. The
  // currently_granted set is carried in step.payload by the backend
  // plan builder for ``active`` status only — for not_installed /
  // oauth_pending the field is absent and we keep the OAuth step.
  const currentlyGranted = ((step.payload || {}).currently_granted) || [];
  const newCaps = picked.filter(c => !currentlyGranted.includes(c));
  const skipOauth = currentlyGranted.length > 0 && newCaps.length === 0;

  // Override the remaining oauth + complete steps' payloads with the
  // picked capability set. The server's plan returned defaults; we
  // replace them now that the operator has chosen.
  let remaining = (_skillInstallState.pickerRemainingSteps || []).map(s => {
    const clone = Object.assign({}, s, {
      payload: Object.assign({}, s.payload || {}),
    });
    if (clone.id === 'oauth') {
      clone.payload.services = _googleServicesForCapabilities(picked);
    } else if (clone.id === 'complete') {
      clone.payload.capabilities = picked.slice();
    }
    return clone;
  });
  if (skipOauth) {
    remaining = remaining.filter(s => s.id !== 'oauth');
  }
  _skillInstallState.pickerRemainingSteps = null;

  // Drive the rest of the plan.
  btn.disabled = true;
  btn.textContent = 'Continuing…';
  document.getElementById('skill-install-screen-form').style.display = 'none';
  await _skillInstallDrivePlan({ steps: remaining });
}

// Map capability ids → OAuth service ids the existing
// /api/admin/onboard/google/begin route expects. Mirrors the server-side
// _CAPABILITY_INDEX + services field in google_install.GOOGLE_CAPABILITIES.
// Kept in sync via the test in test_skills_google_install.py — if a new
// capability lands, this table must be extended.
const _GOOGLE_CAPABILITY_SERVICES = {
  gmail_read:      ['gmail_readonly'],
  gmail_send:      ['gmail'],
  calendar_read:   ['calendar_readonly'],
  calendar_write:  ['calendar'],
  drive_read:      ['drive_readonly'],
  drive_write:     ['drive'],
  sheets_read:     ['sheets_readonly'],
  sheets_write:    ['sheets'],
  docs_read:       ['docs_readonly'],
  docs_write:      ['docs'],
  gmail_modify:    ['gmail_modify'],
  drive_full:      ['drive_full'],
};

function _googleServicesForCapabilities(capIds) {
  const seen = new Set();
  const out = [];
  for (const id of (capIds || [])) {
    for (const svc of (_GOOGLE_CAPABILITY_SERVICES[id] || [])) {
      if (!seen.has(svc)) { seen.add(svc); out.push(svc); }
    }
  }
  return out;
}

async function skillInstallSubmitForm() {
  // Unified Google skill (post-PR #2154 review): the capability picker
  // re-uses the form screen but its submit handler is different — it
  // doesn't POST to the backend (picker is UI-only), it stores the
  // selection and continues plan execution.
  if (_skillInstallState.pendingPickerStep) {
    return _skillInstallSubmitCapabilityPicker();
  }
  const step = _skillInstallState.pendingFormStep;
  if (!step || !step.endpoint) return;

  const errEl = document.getElementById('skill-install-form-error');
  const btn   = document.getElementById('skill-install-form-submit-btn');
  errEl.textContent = '';

  // Collect field values.
  const payload = { bot_id: _skillInstallState.botId };
  let missing = null;
  for (const f of step.fields || []) {
    const el = document.getElementById('skill-install-form-field-' + f.name);
    const v = (el && el.value || '').trim();
    if (!v) { missing = f.label || f.name; break; }
    payload[f.name] = v;
  }
  if (missing) {
    errEl.textContent = `${missing} is required.`;
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Connecting…';

  let resp;
  try {
    resp = await api('POST', step.endpoint, payload);
  } catch (e) {
    errEl.textContent = _skillInstallFriendlyError(e && (e.message || String(e)), null);
    btn.disabled = false;
    btn.textContent = 'Save and connect';
    return;
  }

  if (!resp || !resp.ok) {
    errEl.textContent = _skillInstallFriendlyError(
      resp && resp.error, resp && resp.status,
    );
    btn.disabled = false;
    btn.textContent = 'Save and connect';
    return;
  }

  // Success — clear form state and show the result screen.
  _skillInstallState.pendingFormStep = null;
  _skillInstallState.formRemainingSteps = null;

  document.getElementById('skill-install-screen-form').style.display = 'none';
  // Build a friendly tail using whatever the backend returned about the
  // remote: HA gives ha_version; Notion gives workspace_name. Falls back
  // to nothing for skills that don't surface either.
  let tail = '';
  if (resp.ha_version) tail = ` (v${escapeHtml(resp.ha_version)})`;
  else if (resp.workspace_name) tail = ` in ${escapeHtml(resp.workspace_name)}`;
  const skillTitle = (document.getElementById('skill-install-title')?.textContent || '')
    .replace(/^Add /, '').split(' to ')[0] || _skillInstallState.skillId;
  _skillInstallShowResult(true, 'All set',
    `This bot can now talk to ${escapeHtml(skillTitle)}${tail}.`);
}

function _skillInstallFriendlyError(rawError, status) {
  // Map backend error strings to Plex-test-friendly messages. Most
  // codes are shared across credential-paste skills (Home Assistant,
  // Notion, future paste-style); a few have skill-specific phrasing.
  const skillId = (_skillInstallState && _skillInstallState.skillId) || '';

  if (rawError === 'invalid_url_format') {
    return 'That address does not look right. Use the form http://homeassistant.local:8123';
  }
  if (rawError === 'invalid_token_format') {
    if (skillId === 'notion') {
      return 'That key does not look like a Notion integration secret. It should start with secret_ or ntn_.';
    }
    return 'That access key looks too short — copy the full long-lived token from the source.';
  }
  if (status === 'revoked' || rawError === 'unauthorized') {
    if (skillId === 'notion') {
      return 'Notion rejected that key. Open notion.so → My integrations and copy a fresh Internal Integration Secret.';
    }
    return 'That key was rejected. Generate a fresh one and paste it here.';
  }
  if (status === 'unreachable' || rawError === 'connection_failed') {
    if (skillId === 'home_assistant') {
      return 'Could not reach Home Assistant at that address from this machine. Check it is on and the URL is right.';
    }
    return 'Could not reach the service. Check your network connection and try again.';
  }
  if (status === 'invalid') {
    return 'Those values did not pass a basic format check. Double-check and try again.';
  }
  if (typeof rawError === 'string' && rawError.startsWith('http_error_')) {
    return `The service returned an unexpected response (${rawError.slice(11)}). Check your values and try again.`;
  }
  return rawError || 'Could not complete setup. Check the values and try again.';
}

async function _skillInstallDrivePlan(plan) {
  const steps = plan.steps || [];
  const errEl = document.getElementById('skill-install-progress-error');
  errEl.textContent = '';

  for (let i = 0; i < steps.length; i++) {
    _skillInstallRenderProgress(steps, i);
    const step = steps[i];

    if (step.id === 'configure_oauth_client') {
      // Hand off to the existing GWS wizard for the one-time pod setup.
      closeSkillInstall();
      if (typeof openGoogleWorkspaceWizard === 'function') {
        openGoogleWorkspaceWizard(_skillInstallState.botId);
      } else {
        toast('Open Plugins → Google Workspace to finish setup', 'warn');
      }
      return;
    }

    if (step.id === 'manual_setup') {
      // Upstream-plugin skills (brave, gdrive, dropbox) defer the
      // actual install to deploy or wizard machinery. The step label IS the
      // install hint — show it as the result and stop driving the plan.
      _skillInstallShowResult(false, 'Manual setup needed', step.label);
      return;
    }

    if (step.id === 'open_github_backup_wizard') {
      // GitHub-specific: hand off to the same guided wizard the operator
      // would launch from Backup → Cloud. Closes this modal, then opens
      // the wizard pre-targeted at this bot. The wizard handles PAT
      // verify, repo create/reuse, deploy key, and .git/config wiring.
      closeSkillInstall();
      const botId = (step.payload && step.payload.bot_id) || _skillInstallState.botId;
      if (typeof openOnboardModal === 'function' && botId) {
        openOnboardModal('github', [botId]);
      } else {
        toast('Open Backup → Cloud → Set up GitHub backup to finish.', 'warn');
      }
      return;
    }

    if (step.id === 'set_config') {
      // Credential-paste skills (Home Assistant LLAT, Notion internal-
      // integration token) render a dynamic form from step.fields[].
      // Submission picks up the rest of the plan via skillInstallSubmitForm.
      _skillInstallState.pendingFormStep = step;
      _skillInstallState.formRemainingSteps = steps.slice(i + 1);
      _skillInstallRenderForm(step);
      return;
    }

    if (step.id === 'set_token') {
      // Telegram-style: BotFather key paste with backend verification
      // (getMe), no OAuth round-trip. The backend's set_token step carries
      // an endpoint + payload but no fields[]; synthesize a single
      // password-style field so the existing form renderer can carry it.
      // skillInstallSubmitForm posts to step.endpoint
      // (= /api/skills/install/telegram/set-token) with {bot_id, bot_token}.
      const formStep = Object.assign({}, step, {
        fields: [{
          name: 'bot_token',
          label: 'BotFather key',
          type: 'password',
          placeholder: '123456789:ABCdefGHI-jkl...',
          help: 'Paste the full token @BotFather gave you. Format: <numbers>:<letters_and_dashes> (around 45 chars).',
        }],
      });
      _skillInstallState.pendingFormStep = formStep;
      _skillInstallState.formRemainingSteps = steps.slice(i + 1);
      _skillInstallRenderForm(formStep);
      return;
    }

    if (step.id === 'enable_plugin') {
      const r = await api('POST',
        '/api/skills/install/' + encodeURIComponent(_skillInstallState.skillId)
          + '/enable-plugin',
        { bot_id: _skillInstallState.botId });
      // _operator_proposal_response returns {ok, proposal_id, status,
      // applied, message}. We treat applied===true (status==='succeeded')
      // as the only happy path; everything else is a refusal we surface.
      if (!r || !r.applied) {
        const reason = (r && (r.message || r.error)) ||
          'plugin enable refused';
        errEl.textContent = `Could not turn on the Google plugin: ${reason}`;
        _skillInstallRenderProgress(steps, i); // leave step as 'doing' → re-render shows error
        return;
      }
      continue;
    }

    if (step.id === 'oauth') {
      // Hand off to the existing GWS OAuth wizard. The wizard handles
      // browser popup + state polling; when it closes, we poll for active.
      _skillInstallRenderProgress(steps, i);
      if (typeof openGoogleWorkspaceWizard === 'function') {
        // Close our modal first so the user sees the GWS modal cleanly.
        closeSkillInstall();
        openGoogleWorkspaceWizard(_skillInstallState.botId);
        // Re-open ourselves once the user finishes/cancels the OAuth flow
        // by polling status in the background — the modal stays closed
        // and the keys page refresh (which closeGoogleWorkspaceWizard
        // calls) is enough confirmation. The follow-up confirmation step
        // is reachable by re-opening this dialog if needed.
      } else {
        errEl.textContent = 'OAuth wizard not available in this build.';
      }
      return;
    }

    if (step.id === 'account_type' || step.id === 'capability_review') {
      // Workspace-suite v1: auto-advance these informational steps.
      // The intro screen already renders the access panel, so the user
      // has seen what the bot will be able to do. v1.5 will turn these
      // into real screens with the path-A / path-B branch + restricted-
      // scope Advanced disclosure (per docs/spec-google-workspace-suite-
      // 2026-06-04.md §4.1).
      _skillInstallRenderProgress(steps, i);
      continue;
    }

    if (step.id === 'pick_capabilities') {
      // Unified Google skill (post-PR #2154 review). Render the per-app
      // capability checkbox sheet; sensible defaults are pre-checked by
      // the server (gmail_read + calendar_read). The submit handler
      // captures the operator's selection and uses it to override the
      // remaining oauth + complete steps' payloads with the picked set.
      _skillInstallState.pendingPickerStep = step;
      _skillInstallState.pickerRemainingSteps = steps.slice(i + 1);
      _skillInstallRenderCapabilityPicker(step);
      return;
    }

    if (step.id === 'complete') {
      // Workspace-suite post-OAuth provisioning. Runs preflight +
      // keystore writes + token shim + InstallMcpServer + kickstart.
      // Five sub-steps; the response carries per-step pass/fail so the
      // result screen renders the diagnostic table.
      _skillInstallRenderProgress(steps, i);
      const r = await api('POST', step.endpoint,
        { bot_id: _skillInstallState.botId });
      if (!r || r.error || !r.ok) {
        _skillInstallRenderGwsCompleteFailure(r);
        return;
      }
      const name = (_skillInstallState && _skillInstallState.displayName)
                 || _skillInstallState.skillId
                 || 'this skill';
      _skillInstallRenderGwsCompleteSuccess(r, name);
      return;
    }

    if (step.id === 'confirm') {
      // Poll for active state, up to 90s. Surface either success or a
      // descriptive holding message.
      _skillInstallRenderProgress(steps, i);
      const ok = await _skillInstallPollUntilActive(90000);
      const name = (_skillInstallState && _skillInstallState.displayName)
                 || _skillInstallState.skillId
                 || 'this skill';
      if (ok) {
        _skillInstallShowResult(true,
          'All set',
          `This bot can now use ${name}.`);
      } else {
        _skillInstallShowResult(false,
          'Still working on it',
          `${name} setup is not complete yet. You can close this and re-open to check status.`);
      }
      return;
    }
  }
  // Plan exhausted without a terminal step (i.e. status was already active
  // but we got here anyway) — show success.
  {
    const name = (_skillInstallState && _skillInstallState.displayName)
               || _skillInstallState.skillId
               || 'this skill';
    _skillInstallShowResult(true,
      'All set',
      `This bot can now use ${name}.`);
  }
}

async function _skillInstallPollUntilActive(timeoutMs) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const st = await api(
        'GET',
        '/api/skills/install/' + encodeURIComponent(_skillInstallState.skillId)
          + '/status?bot_id=' + encodeURIComponent(_skillInstallState.botId),
      );
      // 'active' = GOG completion state; 'valid' = Slack (V2.1-2) completion state.
      if (st && st.ok && (st.status === 'active' || st.status === 'valid')) return true;
    } catch (_) { /* keep polling */ }
    await new Promise(r => setTimeout(r, 2500));
  }
  return false;
}

// ── Slack OAuth driver (V2.1-2 endpoint contract) ───────────────────────────
// 1. POST /api/skills/install/slack/start-oauth → { authorize_url, state }
// 2. Open authorize_url in a popup window.
// 3. Wait for postMessage(type: 'slack-oauth') from the callback page OR a
//    terminal /api/skills/install/slack/poll response — whichever fires first.
// 4. Show success / failure in the result screen.
//
// Bypasses the V2.1-4 awaiting_oauth orchestrator path because Slack's status
// ('valid') doesn't match the orchestrator's 'active' completion check and
// the OAuth round-trip is popup-based, not action_url-based.
async function _slackInstallRunOAuth() {
  // Clear any leftover error from a previous open of the modal.
  const errEl = document.getElementById('skill-install-progress-error');
  if (errEl) errEl.textContent = '';

  let startResp;
  try {
    startResp = await api('POST', '/api/skills/install/slack/start-oauth',
      { bot_id: _skillInstallState.botId });
  } catch (e) {
    _skillInstallShowResult(false, 'Could not start Slack authorization',
      (e && e.message) || String(e));
    return;
  }
  if (!startResp || !startResp.ok || !startResp.authorize_url) {
    if (startResp && startResp.error === 'slack_credentials_not_configured') {
      _skillInstallShowResult(false, 'Slack app not configured',
        startResp.hint ||
        'Pod admin needs to register a Slack app first. See docs/skills/slack-setup.md.');
    } else {
      _skillInstallShowResult(false, 'Could not start Slack authorization',
        (startResp && startResp.error) || 'unknown error');
    }
    return;
  }

  _skillInstallState.slackOAuthState = startResp.state;
  const popup = window.open(
    startResp.authorize_url,
    '_blank',
    'noopener=no,noreferrer=no,popup=yes,width=520,height=720',
  );
  if (!popup) {
    _skillInstallShowResult(false, 'Popup blocked',
      'Allow popups for this site and try again.');
    return;
  }
  _skillInstallState.slackPopupRef = popup;

  const result = await _slackInstallWaitForOAuth(startResp.state, 180000);

  // Tear down the popup + listener — _skillInstallCleanup also runs on
  // closeSkillInstall(), so this is belt-and-suspenders.
  if (_skillInstallState.slackPopupRef) {
    try { _skillInstallState.slackPopupRef.close(); } catch (_) {}
    _skillInstallState.slackPopupRef = null;
  }
  if (_skillInstallState.slackMsgListener) {
    window.removeEventListener('message', _skillInstallState.slackMsgListener);
    _skillInstallState.slackMsgListener = null;
  }
  _skillInstallState.slackOAuthState = null;

  if (!result || result.status !== 'success') {
    const detail =
      !result                       ? 'Slack authorization did not complete.' :
      result.status === 'denied'    ? 'You declined the Slack authorization.' :
      result.status === 'timeout'   ? 'Timed out waiting for Slack authorization.' :
      result.status === 'expired'   ? 'The authorization session expired — try again.' :
      (result.error || 'Slack authorization failed.');
    _skillInstallShowResult(false, 'Slack not connected', detail);
    return;
  }

  const workspaceLabel = result.workspace_name || result.workspace_id || '';
  _skillInstallShowResult(true, 'All set',
    workspaceLabel
      ? `This bot can now send messages to your Slack workspace (${workspaceLabel}).`
      : 'This bot can now send messages to your Slack workspace.');
}

// Wait for the Slack OAuth callback to deliver a terminal poll result.
// Drives postMessage + /poll in parallel so we win whichever path fires.
function _slackInstallWaitForOAuth(state, timeoutMs) {
  return new Promise(resolve => {
    let done = false;
    let pollTimer = null;
    let timeoutTimer = null;

    const finish = (val) => {
      if (done) return;
      done = true;
      if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
      if (timeoutTimer) { clearTimeout(timeoutTimer); timeoutTimer = null; }
      resolve(val);
    };

    const pollOnce = async () => {
      if (done) return;
      let r;
      try {
        r = await api('POST', '/api/skills/install/slack/poll', { state });
      } catch (_) {
        pollTimer = setTimeout(pollOnce, 2000);
        return;
      }
      if (done) return;
      if (r && r.pending) {
        pollTimer = setTimeout(pollOnce, 1500);
        return;
      }
      finish(r);
    };

    // Fast path: callback page postMessages us the moment it lands.
    _skillInstallState.slackMsgListener = (ev) => {
      if (!ev || !ev.data || ev.data.type !== 'slack-oauth') return;
      // postMessage may fire a hair before the poll result is observable;
      // a short delay lets the callback's state-write settle.
      setTimeout(pollOnce, 50);
    };
    window.addEventListener('message', _skillInstallState.slackMsgListener);

    timeoutTimer = setTimeout(() => finish({ status: 'timeout' }), timeoutMs || 180000);
    pollOnce();
  });
}

function _skillInstallShowResult(ok, message, detail) {
  document.getElementById('skill-install-screen-intro').style.display = 'none';
  document.getElementById('skill-install-screen-progress').style.display = 'none';
  document.getElementById('skill-install-screen-result').style.display = 'block';
  document.getElementById('skill-install-result-icon').textContent = ok ? '✅' : '⚠️';
  document.getElementById('skill-install-result-msg').textContent = message || '';
  document.getElementById('skill-install-result-detail').textContent = detail || '';
}

// ── Workspace-suite /complete result rendering ─────────────────────────────
//
// The POST /api/skills/install/google_workspace_{read,write}/complete
// endpoints return a CompletionResult dict with five sub-step records
// (preflight / keystore / token_shim / mcp_install / gateway_kickstart).
// On success we show the green tick; on failure we surface the per-step
// breakdown so the operator sees exactly which stage failed — matches the
// "distinguish tooling failure from substantive findings" memory.

function _skillInstallRenderGwsCompleteSuccess(response, skillName) {
  const account = response.google_account || '';
  const accountSuffix = account ? ` as ${account}` : '';
  const detailLines = [
    `${skillName} is now connected${accountSuffix}.`,
  ];
  if (response.gateway_kickstart && !response.gateway_kickstart.done) {
    detailLines.push(
      `Note: the bot's gateway needs a restart — ${response.gateway_kickstart.error || 'kickstart failed'}.`,
    );
  }
  _skillInstallShowResult(true, 'All set', detailLines.join(' '));
}

function _skillInstallRenderGwsCompleteFailure(response) {
  // Order matches the actual provisioning sequence so the operator
  // sees them in execution order.
  const stages = [
    { key: 'preflight',          label: 'Check Gmail, Calendar, and Drive access' },
    { key: 'keystore',           label: 'Save OAuth client credentials' },
    { key: 'token_shim',         label: 'Write the Workspace credentials file' },
    { key: 'mcp_install',        label: 'Install the Workspace tool server' },
    { key: 'gateway_kickstart',  label: 'Restart the bot gateway' },
  ];

  // Transient preflight failure (network/deadline) ≠ "the bot's access is
  // broken". The backend marks preflight.transient when the check couldn't
  // RUN. In that case the all-✗ breakdown is misleading — every later stage
  // shows ✗ only because the first one never finished — so show a focused
  // "try again" instead. (Distinguish tooling failure from a real finding.)
  const preflight = (response || {}).preflight || {};
  if (!preflight.done && preflight.transient) {
    _skillInstallShowResult(
      false,
      "Couldn't verify Google access right now",
      'This looks like a temporary network hiccup, not a problem with the '
        + "bot's setup — nothing was changed. Try again in a moment.",
    );
    return;
  }

  const rows = stages.map(s => {
    const stage = (response || {})[s.key] || {};
    const done = !!stage.done;
    const icon = done ? '✓' : '✗';
    const err = stage.error || '';
    const errSuffix = (!done && err) ? `  — ${err}` : '';
    return `${icon}  ${s.label}${errSuffix}`;
  });
  // Find the first failing stage — that's the headline.
  const firstFail = stages.find(s => {
    const stage = (response || {})[s.key] || {};
    return !stage.done;
  });
  const headline = firstFail
    ? `Couldn't ${firstFail.label.toLowerCase()}.`
    : 'Setup failed.';
  const detail = rows.join('\n');
  _skillInstallShowResult(false, headline, detail);
}

function closeSkillInstall() {
  _skillInstallCleanup();
  document.getElementById('skill-install-modal').classList.remove('open');
  // Refresh the catalog so the per-bot install buttons reflect any change
  // we just made (e.g. a Home Assistant token-paste flips a bot from
  // "+ Add" to "✓"). Cheap call — no-op if the user wasn't on the catalog tab.
  if (typeof skillsLoadCatalog === 'function' &&
      document.getElementById('skills-view-catalog')?.style.display !== 'none') {
    skillsLoadCatalog(true);
  }
}

function _skillInstallCleanup() {
  if (_skillInstallState.pollTimer) {
    clearTimeout(_skillInstallState.pollTimer);
    _skillInstallState.pollTimer = null;
  }
  if (_skillInstallState.slackMsgListener) {
    window.removeEventListener('message', _skillInstallState.slackMsgListener);
    _skillInstallState.slackMsgListener = null;
  }
  if (_skillInstallState.slackPopupRef) {
    try { _skillInstallState.slackPopupRef.close(); } catch (_) {}
    _skillInstallState.slackPopupRef = null;
  }
  _skillInstallState.slackOAuthState = null;
}


// ══════════════════════════════════════════════════════
// Shared awaiting_oauth install-plan renderer (V2.1-4)
// Used by both the Skills page install flow and the Gallery install flow.
// ══════════════════════════════════════════════════════

// Registry mapping integration_id → in-page launcher. When a missing-item
// has an entry here, the awaiting_oauth panel renders its button as a
// <button onclick> calling the launcher instead of as an <a href>
// navigating to action_url.
//
// Why this exists: the orchestrator's action_url for OAuth-requiring
// skills (gog/gmail/calendar) is the POST-only /api/skills/install/<skill>
// endpoint — that's where the wizard POSTs to advance the install plan.
// Rendering it as a plain <a href> causes the browser to issue a GET
// on the same URL, which returns {"error":"method not allowed"} (405).
// Operators landed on a broken JSON page with no recovery; only fix
// was to close + reopen the app. Surfaced 2026-06-01.
//
// For known OAuth-style skills we dispatch to the matching in-page
// wizard (openGoogleWorkspaceWizard for any Google skill), closing
// the skill-install modal first so the operator sees the destination
// wizard cleanly. Skills without a launcher entry fall back to the
// legacy <a href> behavior — kept as a safety net but new OAuth
// providers should register a launcher here.
const _AWAITING_OAUTH_LAUNCHERS = {
  gog:      (botId) => (typeof openGoogleWorkspaceWizard === 'function') && openGoogleWorkspaceWizard(botId),
  gmail:    (botId) => (typeof openGoogleWorkspaceWizard === 'function') && openGoogleWorkspaceWizard(botId),
  calendar: (botId) => (typeof openGoogleWorkspaceWizard === 'function') && openGoogleWorkspaceWizard(botId),
  // Unified Google skill (post-PR #2154). The skill-install modal's
  // capability-picker step → oauth → complete drives the whole flow.
  google: (botId) => (typeof openSkillInstall === 'function') && openSkillInstall('google', botId),
  // Backward-compat for any deep-links that still reference the split skills.
  google_workspace_read:  (botId) => (typeof openSkillInstall === 'function') && openSkillInstall('google', botId),
  google_workspace_write: (botId) => (typeof openSkillInstall === 'function') && openSkillInstall('google', botId),
};

// Top-level entry point invoked from the inline `onclick` rendered by
// _renderAwaitingOauthInstallPlan. We close the parent skill-install
// modal first (so the destination wizard doesn't render behind it),
// then call the launcher. Toasts if no launcher matches — but the
// renderer only emits this click handler when a launcher exists, so
// that branch is purely defensive.
function _handleAwaitingOauthLauncher(integrationId, botId) {
  const launcher = _AWAITING_OAUTH_LAUNCHERS[integrationId];
  if (!launcher) {
    toast(`No in-page launcher registered for ${integrationId}`, 'err');
    return;
  }
  if (typeof closeSkillInstall === 'function') {
    closeSkillInstall();
  }
  launcher(botId);
}

/**
 * Render a "waiting for OAuth" panel into renderTarget and poll statusEndpoint
 * until the skill/integration becomes active.
 *
 * @param {Array}    missingList      Array of missing-item dicts from the orchestrator
 *                                     (shape: {display_name, reason, action_url, action_label,
 *                                               install_plan_steps, status})
 * @param {string}   statusEndpoint   URL to GET for polling completion (expects {ok, status})
 * @param {Function} onResume         Called when status becomes "active" or "already_active"
 * @param {Function} onTimeout        Called when the poll window (90 s) expires
 * @param {Element}  renderTarget     DOM element to render into
 * @param {string}   botId            Optional bot id; required for the in-page launcher dispatch.
 *                                     Both call sites have it in scope; defaults to
 *                                     _skillInstallState.botId when omitted (Skills page only).
 */
function _renderAwaitingOauthInstallPlan(missingList, statusEndpoint, onResume, onTimeout, renderTarget, botId) {
  if (!renderTarget) return;
  // botId is the new (sixth) argument for the JS-launcher dispatch.
  // Most call sites already have a bot_id in scope; passing it
  // through avoids reaching into _skillInstallState (which is only
  // set when the Skills page drives the flow — not the Gallery).
  const _effectiveBotId = botId
    || (typeof _skillInstallState !== 'undefined' && _skillInstallState && _skillInstallState.botId)
    || '';

  // Build the panel HTML
  let html = `<div style="padding:4px 0 12px">
    <div style="font-size:0.85rem;color:var(--text2);margin-bottom:10px">
      Complete the steps below to finish setup:
    </div>`;

  (missingList || []).forEach((item, idx) => {
    const name   = escapeHtml(item.display_name || item.integration_id || item.skill_id || '');
    const reason = escapeHtml(item.reason || '');
    const url    = item.action_url || '';
    const label  = escapeHtml(item.action_label || 'Set up ' + name);
    const steps  = item.install_plan_steps || [];
    const integrationId = item.integration_id || item.skill_id || '';
    const hasJsLauncher = !!_AWAITING_OAUTH_LAUNCHERS[integrationId];

    html += `<div style="border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:10px">
      <div style="font-weight:600;font-size:0.9rem;margin-bottom:4px">${name}</div>`;
    if (reason) {
      html += `<div style="font-size:0.78rem;color:var(--text3);margin-bottom:8px">${reason}</div>`;
    }
    // Show plan steps if present (informational, not interactive)
    if (steps.length) {
      html += `<ol style="font-size:0.78rem;color:var(--text2);margin:0 0 8px 16px;padding:0">`;
      steps.forEach(s => {
        html += `<li style="margin-bottom:3px">${escapeHtml(s.label || s.id || '')}</li>`;
      });
      html += `</ol>`;
    }
    if (hasJsLauncher && _effectiveBotId) {
      // In-page launcher: clicking opens the destination wizard. This
      // branch handles OAuth skills (gog/gmail/calendar) whose
      // action_url is a POST-only API endpoint that browsers can't
      // GET safely (would 405).
      html += `<button type="button" class="btn btn-primary btn-sm"
        style="margin-top:4px"
        onclick='_handleAwaitingOauthLauncher(${JSON.stringify(integrationId)}, ${JSON.stringify(_effectiveBotId)})'>${label}</button>`;
    } else if (url) {
      // Fallback: navigate to URL. Kept for non-API action_urls that
      // are actually safe to GET (none today, but a forward-compat
      // hedge). If a future skill lands here and 405s, add it to
      // _AWAITING_OAUTH_LAUNCHERS above.
      html += `<a href="${escapeHtml(url)}" class="btn btn-primary btn-sm"
        style="display:inline-block;margin-top:4px;text-decoration:none">${label}</a>`;
    }
    html += `</div>`;
  });

  html += `<div style="font-size:0.78rem;color:var(--text3);margin-top:4px">
    This panel will update automatically once setup is complete.
  </div></div>`;

  renderTarget.innerHTML = html;

  // Poll statusEndpoint every 3 s for up to 90 s
  const pollStart = Date.now();
  const POLL_TIMEOUT = 90000;
  const POLL_INTERVAL = 3000;

  let _pollHandle = null;

  async function _poll() {
    if (Date.now() - pollStart >= POLL_TIMEOUT) {
      if (typeof onTimeout === 'function') onTimeout();
      return;
    }
    try {
      const st = await api('GET', statusEndpoint);
      // 'active'/'already_active' = GOG and shared completion states;
      // 'valid' = Slack (V2.1-2) completion state.
      if (st && st.ok && (st.status === 'active' || st.status === 'already_active' || st.status === 'valid')) {
        if (typeof onResume === 'function') onResume();
        return;
      }
    } catch (_) { /* keep polling */ }
    _pollHandle = setTimeout(_poll, POLL_INTERVAL);
  }

  // Kick off the first poll after one interval
  _pollHandle = setTimeout(_poll, POLL_INTERVAL);

  // Return the handle so callers can cancel if the modal is closed
  return {
    cancel: () => { if (_pollHandle) { clearTimeout(_pollHandle); _pollHandle = null; } },
  };
}

// ══════════════════════════════════════════════════════
// Google Workspace OAuth wizard
// ══════════════════════════════════════════════════════
const _gwsState = {
  botId: null,
  selected: {},          // service id → bool
  scopeRegistry: [],     // from /onboard/google/status
  state: null,           // OAuth state token
  pollTimer: null,
  popupRef: null,
  msgListener: null,
};

async function openGoogleWorkspaceWizard(botId) {
  _gwsState.botId = botId;
  _gwsState.selected = {};
  _gwsState.state = null;
  _gwsCleanupPolling();

  // Title is neutral — this wizard works for BOTH personal @gmail.com
  // accounts AND Workspace @your-company.com accounts. The User type
  // setting in the GCP project (External vs Internal) is what
  // differentiates them, not the wizard itself.
  document.getElementById('gws-wizard-title').textContent =
    `Set up Google for ${botLabel(botId)}`;
  document.getElementById('gws-wizard-subtitle').innerHTML =
    'OAuth flow for any Google account — personal @gmail.com or Workspace @your-company.com. ' +
    'Token refresh is automatic once the app is published.';
  document.getElementById('gws-screen-gcp').style.display = 'none';
  document.getElementById('gws-screen-services').style.display = 'none';
  document.getElementById('gws-screen-result').style.display = 'none';
  document.getElementById('gws-gcp-error').textContent = '';
  document.getElementById('gws-services-error').textContent = '';
  document.getElementById('gws-services-status').textContent = '';
  document.getElementById('gws-redirect-uri').textContent =
    `${location.protocol}//${location.host}/api/admin/onboard/google/callback`;
  document.getElementById('gws-wizard-modal').classList.add('open');

  // Decide which screen to show based on pod-level config + per-bot status.
  const status = await api('GET', '/api/admin/onboard/google/status');
  if (!status || status.error) {
    document.getElementById('gws-screen-gcp').style.display = 'block';
    document.getElementById('gws-gcp-error').textContent = (status && status.error) || 'Could not load OAuth status';
    return;
  }
  _gwsState.scopeRegistry = Array.isArray(status.services) ? status.services : [];
  if (!status.configured) {
    document.getElementById('gws-screen-gcp').style.display = 'block';
    return;
  }
  // Already-configured: jump to service picker.
  await _gwsRenderServicesScreen();
}

function closeGoogleWorkspaceWizard() {
  _gwsCleanupPolling();
  document.getElementById('gws-wizard-modal').classList.remove('open');
  // Reload keys so any new authorization shows.
  if (typeof loadKeys === 'function') loadKeys();
}

function _gwsCleanupPolling() {
  if (_gwsState.pollTimer) {
    clearTimeout(_gwsState.pollTimer);
    _gwsState.pollTimer = null;
  }
  if (_gwsState.msgListener) {
    window.removeEventListener('message', _gwsState.msgListener);
    _gwsState.msgListener = null;
  }
  if (_gwsState.popupRef) {
    try { _gwsState.popupRef.close(); } catch (_) {}
    _gwsState.popupRef = null;
  }
}

function gwsCopyRedirectUri() {
  const t = document.getElementById('gws-redirect-uri').textContent;
  try {
    navigator.clipboard.writeText(t);
    toast('Redirect URI copied', 'ok');
  } catch (_) {
    toast('Copy failed — select and copy manually', 'err');
  }
}

// ── Deep-link walkthrough helpers ───────────────────────────────────────────
// GCP doesn't expose APIs for OAuth client creation; the operator has to
// click through the GCP console. To minimise the agony of finding the
// right page, each step's "Open page →" button deep-links to the exact
// URL with their project ID templated in.
//
// Each button carries data-url-template with the URL pattern; if the
// pattern includes {PROJECT_ID} we substitute the value from the
// gws-project-id input. Buttons that need a project ID also carry the
// ``gws-step-needs-pid`` class so they can be enabled/disabled as the
// input changes (without forcing the operator to type the ID before
// they've created the project).

function gwsOpenStep(btn) {
  const pid = (document.getElementById('gws-project-id') || {}).value || '';
  const tpl = btn.dataset.urlTemplate || '';
  if (!tpl) return;
  const needsPid = btn.classList.contains('gws-step-needs-pid');
  const cleanPid = pid.trim();
  if (needsPid && !cleanPid) {
    toast('Enter your project ID first (you got it from Step 1)', 'warn');
    return;
  }
  // Encode the project ID so a stray space or special char doesn't
  // produce a broken URL. (GCP project IDs are lowercase + dashes +
  // digits, but never trust user input.)
  const url = tpl.replace('{PROJECT_ID}', encodeURIComponent(cleanPid));
  window.open(url, '_blank', 'noopener');
}

function gwsUpdateProjectUrls() {
  const pid = ((document.getElementById('gws-project-id') || {}).value || '').trim();
  const buttons = document.querySelectorAll('.gws-step-needs-pid');
  buttons.forEach(btn => {
    btn.disabled = !pid;
    btn.title = pid ? '' : 'Enter your project ID above first';
  });
}

async function gwsSaveClientConfig() {
  const cid = document.getElementById('gws-client-id').value.trim();
  const cs  = document.getElementById('gws-client-secret').value.trim();
  const errEl = document.getElementById('gws-gcp-error');
  errEl.textContent = '';
  if (!cid || !cs) {
    errEl.textContent = 'Both client ID and secret are required.';
    return;
  }
  const btn = document.getElementById('gws-save-client-btn');
  btn.disabled = true; btn.textContent = 'Saving…';
  // Pass the current bot as ``secret_bot`` so the OAuth client lands
  // in this bot's per-bot slot. Without this the configure call falls
  // back to ``network.primary``, then /begin for this bot can't find
  // the credential (the resolver looks at network.bots[<this bot>].
  // googleOAuthClient first, then pod-level — neither of which would
  // be populated). User-reported bug from the canary install.
  const r = await api('POST', '/api/admin/onboard/google/configure', {
    client_id: cid, client_secret: cs,
    secret_bot: _gwsState.botId,
  });
  btn.disabled = false; btn.textContent = 'Save & Continue';
  if (!r || r.error) {
    errEl.textContent = (r && r.error) || 'Save failed';
    return;
  }
  // Reload status (registry + redirect_uri) and advance.
  const status = await api('GET', '/api/admin/onboard/google/status');
  _gwsState.scopeRegistry = (status && status.services) || _gwsState.scopeRegistry;
  document.getElementById('gws-screen-gcp').style.display = 'none';
  await _gwsRenderServicesScreen();
}

async function _gwsRenderServicesScreen() {
  document.getElementById('gws-screen-services').style.display = 'block';
  // Initialize selected from defaults + any existing per-bot grant.
  const existing = await _gwsLoadCurrentRow();
  const existingServices = new Set(((existing && existing.granted_services) || []));
  _gwsState.selected = {};
  for (const svc of _gwsState.scopeRegistry) {
    _gwsState.selected[svc.id] = existingServices.has(svc.id) || (svc.default_on && !existing);
  }
  document.getElementById('gws-account-current').innerHTML = (existing && existing.google_account)
    ? `Currently authorized: <strong>${escHtml(existing.google_account)}</strong>`
    : 'No account connected yet.';

  const basic = _gwsState.scopeRegistry.filter(s => !s.advanced);
  const advanced = _gwsState.scopeRegistry.filter(s => s.advanced);
  document.getElementById('gws-services-list').innerHTML = basic.map(s => `
    <label style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border)">
      <input type="checkbox" data-service="${escHtml(s.id)}" ${_gwsState.selected[s.id] ? 'checked' : ''} onchange="gwsToggleService('${escHtml(s.id)}', this.checked)">
      <strong>${escHtml(s.label)}</strong>
    </label>
  `).join('');
  document.getElementById('gws-advanced-services-list').innerHTML = advanced.map(s => `
    <label style="display:flex;align-items:center;gap:8px;padding:4px 0">
      <input type="checkbox" data-service="${escHtml(s.id)}" ${_gwsState.selected[s.id] ? 'checked' : ''} onchange="gwsToggleService('${escHtml(s.id)}', this.checked)">
      <strong>${escHtml(s.label)}</strong>
      ${s.restricted ? '<span style="font-size:0.7rem;color:var(--orange)">restricted</span>' : ''}
    </label>
  `).join('');
  if (advanced.length === 0) {
    document.getElementById('gws-advanced-toggle').style.display = 'none';
  }
  _gwsUpdateScopePreview();
}

async function _gwsLoadCurrentRow() {
  if (!_gwsState.botId) return null;
  try {
    const d = await api('GET', `/api/admin/keys/${_gwsState.botId}`);
    return (d && d.keys || []).find(k => k.provider === 'google_workspace') || null;
  } catch (_) {
    return null;
  }
}

function gwsToggleService(id, checked) {
  _gwsState.selected[id] = !!checked;
  _gwsUpdateScopePreview();
}

function gwsToggleAdvanced() {
  const el = document.getElementById('gws-advanced-list');
  const isOpening = el.style.display === 'none';
  el.style.display = isOpening ? 'block' : 'none';
  // Sync the expand-icon rotation with the open/closed state.
  const icon = document.querySelector('#gws-advanced-link .expand-icon');
  if (icon) icon.classList.toggle('is-open', isOpening);
}

function _gwsSelectedServiceIds() {
  return Object.keys(_gwsState.selected).filter(k => _gwsState.selected[k]);
}

function _gwsUpdateScopePreview() {
  const selected = _gwsSelectedServiceIds();
  const scopes = new Set();
  // Mirror the server: openid + email + profile are always included.
  scopes.add('openid'); scopes.add('email'); scopes.add('profile');
  for (const s of _gwsState.scopeRegistry) {
    if (_gwsState.selected[s.id] && Array.isArray(s.scopes)) {
      for (const sc of s.scopes) scopes.add(sc);
    }
  }
  // The server is the source of truth; this preview reproduces it from the
  // registry returned by /onboard/google/status. For services whose scopes
  // aren't in the registry payload we fall back to a label-only line.
  const lines = Array.from(scopes).sort();
  document.getElementById('gws-scope-preview').textContent =
    lines.length ? lines.join('\n') : '(no scopes selected — pick at least one service)';
  document.getElementById('gws-signin-btn').disabled = (selected.length === 0);
}

async function gwsBeginAuth() {
  const services = _gwsSelectedServiceIds();
  const errEl = document.getElementById('gws-services-error');
  const stEl = document.getElementById('gws-services-status');
  errEl.textContent = '';
  stEl.textContent = '';
  if (!services.length) {
    errEl.textContent = 'Pick at least one service.';
    return;
  }
  const btn = document.getElementById('gws-signin-btn');
  btn.disabled = true; btn.textContent = 'Opening Google…';
  const r = await api('POST', '/api/admin/onboard/google/begin', {
    bot_id: _gwsState.botId,
    services,
  });
  if (!r || r.error || !r.authorize_url) {
    btn.disabled = false; btn.textContent = 'Sign in with Google';
    if (r && r.error === 'google_oauth_client_not_configured') {
      // Switch back to GCP-setup screen.
      document.getElementById('gws-screen-services').style.display = 'none';
      document.getElementById('gws-screen-gcp').style.display = 'block';
      document.getElementById('gws-gcp-error').textContent = 'Configure the OAuth client first.';
      return;
    }
    errEl.textContent = (r && r.error) || 'Failed to start OAuth flow';
    return;
  }
  _gwsState.state = r.state;
  // Open Google's consent screen in a new tab.
  _gwsState.popupRef = window.open(r.authorize_url, '_blank', 'noopener=no,noreferrer=no,popup=yes,width=520,height=640');
  if (!_gwsState.popupRef) {
    errEl.textContent = 'Popup blocked. Allow popups and try again.';
    btn.disabled = false; btn.textContent = 'Sign in with Google';
    return;
  }
  stEl.textContent = 'Waiting for Google authorization…';
  // Listen for postMessage from the closing tab (instant) AND poll the
  // server for the result (covers the case where postMessage is blocked).
  _gwsState.msgListener = (ev) => {
    if (!ev || !ev.data || ev.data.type !== 'google-oauth') return;
    _gwsResolveAuth();
  };
  window.addEventListener('message', _gwsState.msgListener);
  _gwsPollOnce();
}

async function _gwsPollOnce() {
  if (!_gwsState.state) return;
  const r = await api('POST', '/api/admin/onboard/google/poll', { state: _gwsState.state });
  if (r && r.pending) {
    _gwsState.pollTimer = setTimeout(_gwsPollOnce, 1500);
    return;
  }
  // Terminal — render result.
  _gwsRenderResult(r);
}

async function _gwsResolveAuth() {
  // Triggered by postMessage. Run a single poll to get the consumed result.
  if (!_gwsState.state) return;
  const r = await api('POST', '/api/admin/onboard/google/poll', { state: _gwsState.state });
  if (r && r.pending) {
    // Race: callback wrote result but not yet observable; retry once.
    setTimeout(_gwsResolveAuth, 400);
    return;
  }
  _gwsRenderResult(r);
}

async function _gwsRenderResult(r) {
  _gwsCleanupPolling();
  document.getElementById('gws-screen-services').style.display = 'none';
  document.getElementById('gws-screen-result').style.display = 'block';
  const iconEl = document.getElementById('gws-result-icon');
  const msgEl = document.getElementById('gws-result-msg');
  const detailEl = document.getElementById('gws-result-detail');
  msgEl.style.color = '';
  detailEl.innerHTML = '';

  // OAuth-leg outcomes that never reach the persistence step.
  if (!r || r.error || r.status === 'expired') {
    _gwsRenderFailure('Authorization timed out',
      (r && (r.error || (r.status === 'expired' ? 'state expired — re-run the wizard' : ''))) || '',
      { retry: true });
    return;
  }
  if (r.status === 'denied') {
    iconEl.textContent = '✕';
    msgEl.style.color = 'var(--orange)';
    msgEl.textContent = 'Authorization denied';
    detailEl.textContent = 'You can re-run the wizard at any time.';
    return;
  }
  if (r.status !== 'success') {
    _gwsRenderFailure('Authorization failed', (r && r.error) || 'Unknown error', { retry: true });
    return;
  }

  // OAuth succeeded — but obtaining a token is NOT the same as the bot being
  // configured. The bot only works once finalize persists the credential to
  // the canonical token store + writes google_integration to network.json
  // (the path google_auth.load_credentials / the bot's Google tools read).
  // So show an in-progress state and DO NOT paint green until finalize
  // confirms persistence. (Live incident 2026-06-21: the wizard showed a
  // green "Authorized" while finalize had silently failed and nothing was
  // persisted — atlas was left unconfigured.)
  const account = r.google_account || _gwsState.botId;
  const services = (r.granted_services || []).join(', ') || '(no services granted)';
  iconEl.textContent = '⏳';
  msgEl.textContent = `Finishing setup for ${account}…`;
  detailEl.textContent = `Granted services: ${services}`;

  let finalize;
  try {
    finalize = await api('POST', '/api/wizard/google/personal/finalize', {
      bot_id: _gwsState.botId,
      consent_screen_state: 'testing',
    });
  } catch (_) {
    finalize = null;
  }

  if (!finalize || finalize.ok !== true || finalize.token_mirrored !== true) {
    // Persistence failed → the bot is NOT configured. A token was obtained
    // but not saved where the bot's Google tools load it. This MUST read as
    // a failure (red), not a green check with an easy-to-miss warning.
    const reason = (finalize && finalize.errors && finalize.errors.length)
      ? finalize.errors[0]
      : 'Could not save the Google credential (network error) — the bot is not configured.';
    _gwsRenderFailure(`Couldn’t finish connecting ${account}`, reason, { retry: true });
    return;
  }

  // Persisted → the bot is configured. Preflight is an advisory live-API
  // check; a preflight failure does not undo the (successful) persistence,
  // so surface it as a warning on top of the connected state, not a failure.
  const pf = finalize.preflight || {};
  if (pf.ok === false && pf.error) {
    iconEl.textContent = '⚠️';
    msgEl.style.color = 'var(--orange)';
    msgEl.textContent = `Connected ${account} — couldn’t verify the token`;
    detailEl.innerHTML = `Granted services: ${escHtml(services)}`
      + `<br><span class="gws-result-note">Preflight warning: ${escHtml(pf.error)}`
      + ' (the credential is saved; re-run if mail access keeps failing)</span>';
    return;
  }
  iconEl.textContent = '✅';
  msgEl.style.color = 'var(--green)';
  msgEl.textContent = `Google connected for ${account}`;
  detailEl.textContent = `Granted services: ${services}`;
}

// Render a red failure state on the result screen, optionally with a
// "Try again" button that returns to the service picker.
function _gwsRenderFailure(title, detail, opts) {
  const iconEl = document.getElementById('gws-result-icon');
  const msgEl = document.getElementById('gws-result-msg');
  const detailEl = document.getElementById('gws-result-detail');
  iconEl.textContent = '❌';
  msgEl.style.color = 'var(--red)';
  msgEl.textContent = title;
  let html = escHtml(detail || '');
  if (opts && opts.retry) {
    html += '<br><button class="btn btn-ghost btn-sm gws-result-retry"'
      + ' onclick="gwsRetryFromResult()">Try again</button>';
  }
  detailEl.innerHTML = html;
}

// "Try again" from a failed result → back to the service picker (the OAuth
// client is still configured; the operator just re-runs consent + finalize).
function gwsRetryFromResult() {
  document.getElementById('gws-screen-result').style.display = 'none';
  _gwsRenderServicesScreen();
}
window.gwsRetryFromResult = gwsRetryFromResult;

async function googleWorkspaceRevoke(botId) {
  if (!await confirmModal({body: `Disconnect Google Workspace from ${botLabel(botId)}? The bot will lose access to Gmail, Calendar, Drive, etc. immediately.`, danger: true})) return;
  const r = await api('POST', '/api/admin/onboard/google/revoke', { bot_id: botId });
  if (!r || r.error) {
    toast('Revoke failed: ' + ((r && r.error) || 'unknown'), 'err');
    return;
  }
  toast(r.revoked ? 'Disconnected.' : 'Disconnected (Google revoke skipped).', 'ok');
  if (typeof loadKeys === 'function') loadKeys();
}

async function submitRollbackKey(botId, profileId, provider, label, fieldKey) {
  if (!await confirmModal({body: `Roll back ${label} to its previous value?`, danger: true})) return;
  const payload = { profile_id: profileId };
  if (fieldKey) payload.field_key = fieldKey;
  const r = await api('POST', `/api/admin/keys/${botId}/${provider}/rollback`, payload);
  if (r.error) {
    toast('Rollback failed: ' + r.error, 'error');
    return;
  }
  toast('Key rolled back.', 'ok');
  loadKeys();
  if (r.requires_restart && r.restart_endpoint) {
    setTimeout(() => _promptGatewayRestart(botId, r.restart_endpoint), 400);
  }
}

async function keysReorder(botId, provider, profileId, direction) {
  // Fetch current key list to determine current order
  const d = await api('GET', `/api/admin/keys/${botId}`);
  if (d.error) { toast('Failed to load keys: ' + d.error, 'error'); return; }
  const providerKeys = (d.keys || []).filter(k => k.provider === provider && k.status === 'active');
  // Sort by current order (nulls last)
  providerKeys.sort((a, b) => (a.order ?? 999) - (b.order ?? 999));
  const ids = providerKeys.map(k => k.profile_id);
  const idx = ids.indexOf(profileId);
  if (idx === -1) return;
  if (direction === 'up' && idx > 0) {
    [ids[idx - 1], ids[idx]] = [ids[idx], ids[idx - 1]];
  } else if (direction === 'down' && idx < ids.length - 1) {
    [ids[idx], ids[idx + 1]] = [ids[idx + 1], ids[idx]];
  }
  const r = await api('PUT', `/api/admin/keys/${botId}/order`, { provider, order: ids });
  if (r.error) { toast('Failed to reorder: ' + r.error, 'error'); return; }
  loadKeys();
}

async function keysRemove(botId, provider, profileId) {
  if (!await confirmModal({body: `Remove this "${provider}" key from ${botLabel(botId)}? This cannot be undone.`, danger: true})) return;
  const r = await api('DELETE', `/api/admin/keys/${botId}/${provider}`, { profile_id: profileId });
  if (r.error) { toast('Failed to remove key: ' + r.error, 'error'); return; }
  toast('Key removed.', 'ok');
  loadKeys();
}

// ══════════════════════════════════════════════════════════════════════════
// Path-C Google Workspace wizard (PR ε)
// Spec: docs/spec-google-integration-paths-2026-05-30.md §7.
//
// Two screens (workspace setup → per-bot config) + a result screen with the
// share-with-bot copy. The workspace-setup screen auto-skips when a SA JSON
// is already installed in the secrets store; the operator can still expand
// it with "+ Install another" to onboard additional Workspace tenants.
// ══════════════════════════════════════════════════════════════════════════

const _gwsCState = {
  botId: null,
  secrets: [],         // [{secret_ref, workspace_domain, client_id, client_email, bots_using}]
  scopes: [],          // catalog [{id, label, description, audience, default_set, high_privilege}]
  defaultScopes: [],
  pendingSaJson: null, // parsed JSON of the in-progress upload, kept in memory
  pendingSaFileName: '',
  current: null,       // bot's current google_integration block (if any)
  forceInstall: false, // true → don't auto-skip Screen 1 even when secrets exist
};

// ══════════════════════════════════════════════════════════════════════
// Unified Google chooser — single entry for Skills page Google adds
// ══════════════════════════════════════════════════════════════════════
//
// Replaces the previous "two redundant buttons in Settings → API Keys
// (Add Gmail/Calendar + Workspace path C), three separate Skills entries
// (gmail/calendar/gog), all routing through different code paths" mess.
// One modal asks the operator what kind of Google account they have,
// then routes to the correct downstream wizard.
//
// Flow:
//   openGoogleUnifiedChooser(botId, skillId)
//      ├─ choice = "workspace"  → openGoogleWorkspaceWizardPathC(botId)
//      │                          [service account + DwD; reuses an installed SA]
//      └─ choice = "personal"   → openSkillInstall('google', botId)
//                                  [unified capability picker → OAuth → complete]
//
// (Legacy gog/gmail/calendar — hidden from the catalog since the PR #2231 IA
// pivot — fall back to the bare openGoogleWorkspaceWizard on the Personal
// branch; they have no capability-picker plan.)
//
// Skills page Google entries all route here via the _GOOGLE_SKILL_IDS check
// in _openSkillInstallOrUnifiedGoogle (the interceptor at the click site).
// The catalog-visible row today is the unified ``google`` skill, so it MUST
// be in that set — otherwise the only clickable Google row skips the chooser
// and hardwires Path-A OAuth, leaving Path C (Workspace SA+DwD) unreachable.

// The unified catalog id. Singled out because its Personal branch re-enters
// openSkillInstall (so the capability picker runs); the hidden legacy ids do
// not have that plan and keep the bare-OAuth fallback.
const _GOOGLE_UNIFIED_SKILL_ID = 'google';
const _GOOGLE_SKILL_IDS = [_GOOGLE_UNIFIED_SKILL_ID, 'gog', 'gmail', 'calendar'];

const _googUnifiedChooserState = {
  botId: '',
  skillId: '',
};

function openGoogleUnifiedChooser(botId, skillId) {
  if (!botId) {
    toast('Pick a bot before configuring Google', 'err');
    return;
  }
  _googUnifiedChooserState.botId = botId;
  // Remember which Google skill triggered the chooser so the Personal branch
  // re-enters the right install flow. Defaults to the unified ``google`` skill
  // (the only catalog-visible Google row).
  _googUnifiedChooserState.skillId = skillId || _GOOGLE_UNIFIED_SKILL_ID;
  const labelEl = document.getElementById('goog-chooser-botlabel');
  if (labelEl) labelEl.textContent = botLabel(botId);

  // Reset state.
  ['workspace', 'personal', 'help'].forEach(v => {
    const input = document.getElementById('goog-chooser-input-' + v);
    if (input) input.checked = false;
  });
  const helpPanel = document.getElementById('goog-chooser-help-panel');
  if (helpPanel) helpPanel.style.display = 'none';
  const errEl = document.getElementById('goog-chooser-error');
  if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }

  document.getElementById('goog-chooser-modal').classList.add('open');

  // Reveal the help panel when the operator picks the "I'm not sure"
  // option, so the decision tree appears inline rather than as a
  // separate screen. Hide on any other choice.
  ['workspace', 'personal', 'help'].forEach(v => {
    const input = document.getElementById('goog-chooser-input-' + v);
    if (input) {
      input.onchange = () => {
        const helpPanel = document.getElementById('goog-chooser-help-panel');
        if (helpPanel) {
          helpPanel.style.display = (v === 'help') ? '' : 'none';
        }
        const errEl = document.getElementById('goog-chooser-error');
        if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }
      };
    }
  });
}

function closeGoogleUnifiedChooser() {
  document.getElementById('goog-chooser-modal').classList.remove('open');
  _googUnifiedChooserState.botId = '';
  _googUnifiedChooserState.skillId = '';
}

function _googleUnifiedChooserContinue() {
  const checked = document.querySelector('input[name="goog-chooser-choice"]:checked');
  const errEl = document.getElementById('goog-chooser-error');
  if (!checked) {
    if (errEl) {
      errEl.textContent = 'Pick Workspace or Personal to continue, or "I\'m not sure" for help.';
      errEl.style.display = '';
    }
    return;
  }
  const choice = checked.value;
  const botId = _googUnifiedChooserState.botId;
  // Capture before closeGoogleUnifiedChooser() clears chooser state below.
  const skillId = _googUnifiedChooserState.skillId || _GOOGLE_UNIFIED_SKILL_ID;

  if (choice === 'help') {
    // The help panel is already visible; nothing to advance to until
    // the operator picks Workspace or Personal.
    if (errEl) {
      errEl.textContent = 'Read the decision tree above, then pick Workspace or Personal.';
      errEl.style.display = '';
    }
    return;
  }

  closeGoogleUnifiedChooser();

  if (choice === 'workspace') {
    if (typeof openGoogleWorkspaceWizardPathC === 'function') {
      openGoogleWorkspaceWizardPathC(botId);
    } else {
      toast('Workspace wizard not available', 'err');
    }
    return;
  }

  if (choice === 'personal') {
    // Personal @gmail.com → the unified capability-picker flow. The plan's
    // informational account_type step auto-advances (the chooser already
    // asked), then pick_capabilities → OAuth → complete runs. Routing
    // straight to the bare openGoogleWorkspaceWizard here would SKIP the
    // capability picker — the Path-A UX regression. Legacy gog/gmail/calendar
    // (hidden from the catalog) have no picker plan, so keep their bare-OAuth
    // path.
    if (skillId === _GOOGLE_UNIFIED_SKILL_ID && typeof openSkillInstall === 'function') {
      openSkillInstall(skillId, botId);
    } else if (typeof openGoogleWorkspaceWizard === 'function') {
      openGoogleWorkspaceWizard(botId);
    } else {
      toast('Personal Gmail wizard not available', 'err');
    }
    return;
  }
}

// Interceptor used by the Skills page Browse rows. When the operator
// clicks "+ Add to <bot>" on a Google skill entry (the catalog-visible
// ``google`` row, or a hidden legacy gog/gmail/calendar row), we open the
// unified account-type chooser instead of jumping straight into the install
// modal. For non-Google skills the call passes through unchanged.
function _openSkillInstallOrUnifiedGoogle(skillId, botId) {
  if (_GOOGLE_SKILL_IDS.indexOf(skillId) !== -1) {
    openGoogleUnifiedChooser(botId, skillId);
    return;
  }
  if (typeof openSkillInstall === 'function') {
    openSkillInstall(skillId, botId);
  }
}

async function openGoogleWorkspaceWizardPathC(botId) {
  if (!botId) {
    toast('Select a bot first (from the bot picker above the Credentials table)', 'err');
    return;
  }
  _gwsCState.botId = botId;
  _gwsCState.pendingSaJson = null;
  _gwsCState.pendingSaFileName = '';
  _gwsCState.forceInstall = false;
  document.getElementById('gws-c-wizard-title').textContent =
    `Set up Google Workspace (path C) — ${botLabel(botId)}`;
  document.getElementById('gws-c-wizard-modal').classList.add('open');

  // Reset visible state on all three screens
  document.getElementById('gws-c-screen-1-error').textContent = '';
  document.getElementById('gws-c-screen-2-error').textContent = '';
  document.getElementById('gws-c-result-ok').style.display = 'none';
  document.getElementById('gws-c-result-fail').style.display = 'none';
  document.getElementById('gws-c-sa-file-info').textContent = '';
  document.getElementById('gws-c-existing-sas').style.display = 'none';
  document.getElementById('gws-c-install-block').style.display = '';
  document.getElementById('gws-c-screen-2-bot-label').textContent = botLabel(botId);

  // Fetch the wizard state in parallel with scope catalog
  try {
    const [state, scopes] = await Promise.all([
      api('GET', '/api/wizard/google/state?bot_id=' + encodeURIComponent(botId)),
      api('GET', '/api/wizard/google/scopes'),
    ]);
    _gwsCState.scopes = (scopes && scopes.scopes) || [];
    _gwsCState.secrets = (state && state.secrets) || [];
    _gwsCState.defaultScopes = (state && state.default_scopes) || [];
    _gwsCState.current = (state && state.bot) || null;

    _gwsCRenderScopesPasteList();

    if (_gwsCState.secrets.length === 0) {
      // First-time setup — start at Screen 1
      _gwsCGotoScreen1();
    } else {
      // SAs already installed; show the chooser banner on Screen 1
      _gwsCRenderExistingSAs();
      document.getElementById('gws-c-existing-sas').style.display = '';
      document.getElementById('gws-c-install-block').style.display = 'none';
      _gwsCGotoScreen1();
    }
  } catch (e) {
    document.getElementById('gws-c-screen-1-error').textContent =
      'Could not load wizard state: ' + ((e && e.message) || e);
    _gwsCGotoScreen1();
  }
}

function closeGoogleWorkspaceWizardPathC() {
  document.getElementById('gws-c-wizard-modal').classList.remove('open');
  // Refresh the Credentials table so any new path-C config shows up
  if (typeof loadKeys === 'function') loadKeys();
}

function _gwsCGotoScreen(n) {
  for (const i of [1, 2, 3]) {
    const screen = document.getElementById('gws-c-screen-' + i);
    const ind = document.getElementById('gws-c-ind-' + i);
    if (screen) screen.style.display = (i === n) ? '' : 'none';
    if (ind) {
      ind.style.fontWeight = (i === n) ? '600' : '400';
      ind.style.color = (i === n) ? 'var(--accent)' :
                        (i < n)   ? 'var(--text2)' : 'var(--text3)';
    }
  }
}

function _gwsCGotoScreen1() { _gwsCGotoScreen(1); }
function _gwsCGotoScreen2() {
  _gwsCRenderScreen2();
  _gwsCGotoScreen(2);
}

function _gwsCToggleInstallAnother() {
  _gwsCState.forceInstall = true;
  document.getElementById('gws-c-install-block').style.display = '';
}

function _gwsCRenderExistingSAs() {
  const el = document.getElementById('gws-c-existing-sas-list');
  el.innerHTML = _gwsCState.secrets.map(s => {
    const using = (s.bots_using || []).length
      ? `<span style="color:var(--text3);font-size:0.72rem"> · used by ${(s.bots_using || []).map(escHtml).join(', ')}</span>`
      : '';
    const domain = s.workspace_domain
      ? `<code style="font-size:0.74rem">${escHtml(s.workspace_domain)}</code>`
      : '<span style="color:var(--text3);font-size:0.74rem">domain unknown</span>';
    return `<div style="padding:3px 0"><b>${escHtml(s.secret_ref)}</b> — ${domain}${using}</div>`;
  }).join('');
}

function _gwsCRenderScopesPasteList() {
  const ids = (_gwsCState.scopes || []).map(s => s.id);
  document.getElementById('gws-c-paste-scopes').textContent = ids.join(',');
}

function _gwsCCopyScopesToClipboard() {
  const t = document.getElementById('gws-c-paste-scopes').textContent;
  try { navigator.clipboard.writeText(t); toast('Scope list copied', 'ok'); }
  catch (_) { toast('Copy failed — select and copy manually', 'err'); }
}

function _gwsCSAFileChanged(ev) {
  const file = ev.target.files && ev.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const parsed = JSON.parse(reader.result);
      _gwsCState.pendingSaJson = parsed;
      _gwsCState.pendingSaFileName = file.name;
      const info = document.getElementById('gws-c-sa-file-info');
      const email = parsed.client_email || '(no client_email)';
      info.textContent = `Parsed: ${email}`;
      info.style.color = parsed.type === 'service_account' ? 'var(--green)' : 'var(--orange)';
      // Pre-fill the secret_ref if blank, derived from project_id
      const refInput = document.getElementById('gws-c-secret-ref');
      if (refInput && !refInput.value.trim() && parsed.project_id) {
        // e.g. "evolve-2026-05-30" → "google-sa-evolve-2026-05-30"
        const safe = String(parsed.project_id).toLowerCase().replace(/[^a-z0-9_-]/g, '-').slice(0, 48);
        refInput.value = 'google-sa-' + safe;
      }
    } catch (e) {
      _gwsCState.pendingSaJson = null;
      _gwsCState.pendingSaFileName = '';
      const info = document.getElementById('gws-c-sa-file-info');
      info.textContent = 'Could not parse as JSON: ' + (e.message || e);
      info.style.color = 'var(--red)';
    }
  };
  reader.readAsText(file);
}

async function _gwsCUploadSA() {
  const errEl = document.getElementById('gws-c-screen-1-error');
  errEl.textContent = '';
  const btn = document.getElementById('gws-c-upload-btn');
  const refVal = document.getElementById('gws-c-secret-ref').value.trim();
  const domainVal = document.getElementById('gws-c-workspace-domain').value.trim().toLowerCase();

  if (!refVal) { errEl.textContent = 'Secret reference name is required.'; return; }
  if (!domainVal) { errEl.textContent = 'Workspace primary domain is required.'; return; }
  if (!_gwsCState.pendingSaJson) {
    errEl.textContent = 'Pick the SA JSON file before continuing.'; return;
  }

  btn.disabled = true;
  btn.textContent = 'Uploading…';
  try {
    const r = await api('POST', '/api/wizard/google/upload-sa', {
      secret_ref: refVal,
      workspace_domain: domainVal,
      sa_json: _gwsCState.pendingSaJson,
    });
    if (!r || !r.ok) {
      errEl.textContent = (r && r.error) || 'Upload failed';
      btn.disabled = false;
      btn.textContent = 'Upload & continue →';
      return;
    }
    // Push the new secret into state and re-render the chooser
    _gwsCState.secrets = [{
      secret_ref: r.secret_ref,
      workspace_domain: r.workspace_domain,
      client_id: r.client_id,
      client_email: r.client_email,
      bots_using: [],
    }, ..._gwsCState.secrets.filter(s => s.secret_ref !== r.secret_ref)];
    toast('Workspace service account installed', 'ok');
    _gwsCGotoScreen2();
  } catch (e) {
    errEl.textContent = 'Upload failed: ' + ((e && e.message) || e);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Upload & continue →';
  }
}

function _gwsCRenderScreen2() {
  // Workspace SA picker
  const sel = document.getElementById('gws-c-secret-select');
  sel.innerHTML = _gwsCState.secrets.map(s => {
    const label = s.workspace_domain
      ? `${s.secret_ref} — ${s.workspace_domain}`
      : s.secret_ref;
    return `<option value="${escHtml(s.secret_ref)}">${escHtml(label)}</option>`;
  }).join('');
  // Default to the most recent (top of list) — or the existing bot config if any
  const currentRef = _gwsCState.current && _gwsCState.current.service_account_secret_ref;
  if (currentRef && _gwsCState.secrets.some(s => s.secret_ref === currentRef)) {
    sel.value = currentRef;
  }
  _gwsCSecretChanged();

  // Subject pre-fill from existing config, else derive bot_id@<workspace>
  const subjectInput = document.getElementById('gws-c-subject');
  if (_gwsCState.current && _gwsCState.current.subject) {
    subjectInput.value = _gwsCState.current.subject;
  } else {
    const chosen = _gwsCState.secrets.find(s => s.secret_ref === sel.value);
    const domain = chosen && chosen.workspace_domain;
    subjectInput.value = domain ? (_gwsCState.botId + '@' + domain) : '';
  }

  // Scopes
  _gwsCRenderScopesPicker();

  // Alias — if a correspondence block already exists, prefill from it.
  // Otherwise default the alias name to the bot's primary_user.name (the
  // natural choice for EA-style bots: "alias = the user we're sending on
  // behalf of"). Falls back to empty when no primary_user is recorded —
  // the wizard already routed through identity capture by then for most
  // bots, but new pods may not.
  const cur = _gwsCState.current || {};
  const alias = cur.alias || cur.persona || {};
  const primaryUser = cur.primary_user || {};
  const nameEl = document.getElementById('gws-c-persona-name');
  const emailEl = document.getElementById('gws-c-persona-email');
  const disclosureEl = document.getElementById('gws-c-persona-disclosure');
  const defaultHint = document.getElementById('gws-c-persona-name-default-hint');
  if (alias.name) {
    nameEl.value = alias.name;
    if (defaultHint) defaultHint.textContent = '(blank = internal-only)';
  } else if (primaryUser.name) {
    // Smart default: prefill with the user's own name. Operator can
    // overwrite if they want a fictional alias instead.
    nameEl.value = primaryUser.name;
    if (defaultHint) {
      defaultHint.textContent = `(pre-filled from primary user — clear it to make this bot internal-only)`;
      defaultHint.style.color = 'var(--text2)';
    }
  } else {
    nameEl.value = '';
    if (defaultHint) defaultHint.textContent = "(blank = internal-only; bot won't be able to send external email)";
  }
  emailEl.value = alias.email_address || '';
  disclosureEl.value = alias.disclosure || 'soft';
  _gwsCUpdateDisclosureWarning();
  nameEl.oninput = _gwsCUpdateDisclosureWarning;
  disclosureEl.onchange = _gwsCUpdateDisclosureWarning;

  // Multi-user bots: surface a hint that per-user rotation is the
  // long-term plan (deliverable C in docs/spec-multi-user-alias-2026-06-01.md).
  // Today the static alias is used for every turn regardless of who
  // initiated it; once C ships, this section will gain a mode toggle.
  const hintEl = document.getElementById('gws-c-alias-hint');
  if (hintEl && cur.multi_user) {
    hintEl.innerHTML = (
      'This is a multi-user bot. The alias below applies to every turn ' +
      'for now. Per-user rotation (so the bot sends as Sam on Sam\'s ' +
      'turns and as Jordan on Jordan\'s) is coming — see ' +
      '<code>docs/spec-multi-user-alias-2026-06-01.md</code>.'
    );
  }
}

function _gwsCUpdateDisclosureWarning() {
  // Surface the ethical-disclosure caution when the alias name matches
  // the primary user's name AND disclosure is "none" — that combination
  // produces mail that looks self-authored, which is misrepresentation
  // to the recipient. The wizard never blocks; it just informs.
  const warn = document.getElementById('gws-c-disclosure-warn');
  if (!warn) return;
  const cur = _gwsCState.current || {};
  const primaryName = (cur.primary_user && cur.primary_user.name) || '';
  const aliasName = document.getElementById('gws-c-persona-name').value.trim();
  const disclosure = document.getElementById('gws-c-persona-disclosure').value;
  if (disclosure === 'none' && primaryName && aliasName &&
      aliasName.toLowerCase() === primaryName.toLowerCase()) {
    warn.textContent = (
      '⚠ Alias is the user\'s own name and disclosure is "none" — mail ' +
      'will look like the user wrote it personally. Pick "soft" or ' +
      '"explicit" unless you have a documented reason not to.'
    );
    warn.style.display = '';
  } else {
    warn.style.display = 'none';
  }
}

function _gwsCSecretChanged() {
  // When the operator picks a different SA, re-derive the subject default
  const sel = document.getElementById('gws-c-secret-select');
  const chosen = _gwsCState.secrets.find(s => s.secret_ref === sel.value);
  const subjectInput = document.getElementById('gws-c-subject');
  if (chosen && chosen.workspace_domain
      && (!subjectInput.value || !subjectInput.value.includes('@'))) {
    subjectInput.value = _gwsCState.botId + '@' + chosen.workspace_domain;
  }
}

function _gwsCRenderScopesPicker() {
  const el = document.getElementById('gws-c-scopes-list');
  const currentScopes = (_gwsCState.current && _gwsCState.current.scopes) || [];
  const selected = new Set(currentScopes.length ? currentScopes : _gwsCState.defaultScopes);
  el.innerHTML = _gwsCState.scopes.map(s => {
    const checked = selected.has(s.id) ? 'checked' : '';
    const warn = s.high_privilege
      ? '<span style="color:var(--orange);font-size:0.72rem;font-weight:600">high-privilege</span>'
      : '';
    return `
      <label style="display:flex;align-items:flex-start;gap:8px;padding:5px 0;cursor:pointer;border-bottom:1px solid var(--border)">
        <input type="checkbox" class="gws-c-scope-check" value="${escHtml(s.id)}" ${checked} style="margin-top:3px">
        <div style="flex:1">
          <div style="font-weight:500">${escHtml(s.label)} ${warn}</div>
          <div style="font-size:0.74rem;color:var(--text3)">${escHtml(s.description)}</div>
          <div style="font-size:0.7rem;color:var(--text3);font-family:ui-monospace,Menlo,monospace">${escHtml(s.id)}</div>
        </div>
      </label>`;
  }).join('');
}

async function _gwsCConfigureBot() {
  const errEl = document.getElementById('gws-c-screen-2-error');
  errEl.textContent = '';

  const sel = document.getElementById('gws-c-secret-select');
  const chosen = _gwsCState.secrets.find(s => s.secret_ref === sel.value);
  if (!chosen) { errEl.textContent = 'Pick a Workspace service account.'; return; }

  const subject = document.getElementById('gws-c-subject').value.trim();
  if (!subject) { errEl.textContent = 'Bot Workspace mailbox is required.'; return; }

  const scopes = Array.from(document.querySelectorAll('.gws-c-scope-check'))
    .filter(cb => cb.checked).map(cb => cb.value);
  if (scopes.length === 0) {
    errEl.textContent = 'Pick at least one scope.'; return;
  }

  const persona = {
    name: document.getElementById('gws-c-persona-name').value.trim(),
    email_address: document.getElementById('gws-c-persona-email').value.trim(),
    disclosure: document.getElementById('gws-c-persona-disclosure').value,
  };
  // Outbound-mail scopes need an alias name. Fail loud here rather than
  // letting the backend reject (the operator already filled the form;
  // an inline error is faster). Mirrors the server-side check in
  // wizard_google_routes._validate_configure_payload — keep the two
  // in sync. Read-only configs (no send/modify) skip this check.
  const OUTBOUND_MAIL_SCOPES = new Set([
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.modify',
  ]);
  const needsAlias = scopes.some(s => OUTBOUND_MAIL_SCOPES.has(s));
  if (!persona.name && needsAlias) {
    errEl.textContent = (
      'Alias name is required for outbound mail. Enter the name vendors '
      + 'and recipients should see in the From header — usually the bot\'s '
      + 'primary user.'
    );
    document.getElementById('gws-c-persona-name').focus();
    return;
  }
  // "none" disclosure requires a justification — surface a prompt up front
  // so the operator doesn't see the validation error after the round-trip.
  if (persona.name && persona.disclosure === 'none') {
    const reason = prompt(
      'Disclosure "none" hides the bot-vs-human marker from recipients. ' +
      'Why is this safe for this bot? (recorded in the audit log)');
    if (!reason || !reason.trim()) {
      errEl.textContent = 'A justification is required for disclosure="none".';
      return;
    }
    persona.disclosure_override_reason = reason.trim();
  }

  const btn = document.getElementById('gws-c-configure-btn');
  btn.disabled = true;
  btn.textContent = 'Verifying…';
  try {
    const r = await api('POST', '/api/wizard/google/configure-bot', {
      bot_id: _gwsCState.botId,
      secret_ref: chosen.secret_ref,
      workspace_domain: chosen.workspace_domain || '',
      subject: subject,
      scopes: scopes,
      persona: persona.name ? persona : {},
    });
    if (!r || !r.ok) {
      const errs = (r && r.errors) || [(r && r.error) || 'configure failed'];
      errEl.innerHTML = errs.map(e => '⚠ ' + escHtml(e)).join('<br>');
      btn.disabled = false;
      btn.textContent = 'Configure & verify →';
      return;
    }

    // Render the result screen
    const pf = r.preflight || {};
    const okEl = document.getElementById('gws-c-result-ok');
    const failEl = document.getElementById('gws-c-result-fail');
    if (pf.ok) {
      const prof = pf.profile || {};
      document.getElementById('gws-c-result-ok-detail').innerHTML =
        `Impersonation verified for <b>${escHtml(prof.emailAddress || subject)}</b>` +
        (prof.messagesTotal !== undefined
          ? ` — mailbox has ${escHtml(String(prof.messagesTotal))} messages, ${escHtml(String(prof.threadsTotal || 0))} threads.`
          : '.');
      okEl.style.display = '';
      failEl.style.display = 'none';
    } else {
      document.getElementById('gws-c-result-fail-detail').textContent =
        pf.error || 'Pre-flight call did not return successfully.';
      okEl.style.display = 'none';
      failEl.style.display = '';
    }

    document.getElementById('gws-c-share-prompt').value = r.share_prompt || '';
    _gwsCGotoScreen(3);
  } catch (e) {
    errEl.textContent = 'Configure failed: ' + ((e && e.message) || e);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Configure & verify →';
  }
}

function _gwsCCopyShareToClipboard() {
  const t = document.getElementById('gws-c-share-prompt').value;
  try { navigator.clipboard.writeText(t); toast('Share message copied', 'ok'); }
  catch (_) { toast('Copy failed — select and copy manually', 'err'); }
}

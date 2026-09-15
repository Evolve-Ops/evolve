// ════════════════════════════════════════════════════════════════════════
// Page: Add-a-Bot wizard (5-screen modal)
//
// Spec: internal/spec-add-bot-wizard-2026-05-28.md §4. Calls PR β backend
// at /api/wizard/*. State machine: screens 1→2→3→4→5. Screen 3 is the
// commit point (bot exists after stage 6 of the substrate pipeline).
// Screens 4-5 are post-commit: Cancel from there just closes without
// rolling back.
//
// Lookups (top of the file): _WIZ_STAGES list (stage labels for the
// progress strip).
//
// Screen 1 — name + description + LLM credential mode
//   _wizUpdateTemplateVisibility, openAddBotModal, _wizApplyMode,
//   closeAddBotModal, _wizGoto, _wizScreen1Next, _wizDebouncedPreview,
//   _wizRunPreview, _wizUpdateLlmField, _wizLlmSourceChanged
//
// Screen 2 — channel + verify reachability
//   _wizScreen2Next, _wizRenderStages, _wizPollJob, _wizShowFailure
//
// Screen 3 — provision (commit point)
//   _wizChannelChanged, _wizDeriveAppliedLlmSource, _wizScreen4Skip,
//   _wizScreen4Apply
//
// Screen 4 — skill install
//   _wizGotoApplicationsPage, _wizGotoSkillsPage
//
// Screen 5 — done + verification gauntlet
//   _wizScreen5Done, _wizRenderReviewSummary, _wizVerifyStart,
//   _wizVerifyPoll, _wizVerifyOnDone, _wizVerifySkip, _wizSwapToDoneActions,
//   _wizRenderDoneActions, _wizScreen5DonePair, _wizScreen5DoneViewTile
//
// Verify-fix modal (the per-tile verification helper):
//   _verifyFixOwnership, _verifyModalOpen, _verifyModalClose,
//   _verifyModalStart, _verifyModalPoll
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - api(), toast(), escHtml(), botLabel() — core/
//   - loadStatus() (Overview) — refreshed after provisioning
//   - openOnboardModal — still inline in main script
//   - openSkillInstall — pages/skill-install.js (Phase 3v)
//   - nav() / window.nav — core/router.js
// ════════════════════════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════
// Add-a-Bot wizard (5-screen)
//
// Spec: internal/spec-add-bot-wizard-2026-05-28.md §4.
// Calls PR β backend at /api/wizard/*. (Earlier the wizard also POSTed
// to /api/credentials/borrow from Screen 4 — removed 2026-06-02
// because a hidden default-checked radio caused silent sibling-key
// joins regardless of the operator's Screen-2 choice.)
// Polls the standard /api/jobs/<id> for provision progress
// (wired to provision_bot's on_stage callback in wizard_routes.py).
//
// State machine: screens 1→2→3→4→5. Screen 3 is the commit point
// (bot exists after stage 6 of the substrate pipeline). Screens 4-5
// are post-commit: Cancel from there just closes without rolling
// back (per spec Q3 resolution).
// ══════════════════════════════════════════════════════

const _WIZ_STAGES = [
  { key: 'validate_inputs',     label: 'Validate inputs' },
  { key: 'create_macos_user',   label: 'Create macOS account' },
  { key: 'create_openclaw_dir', label: 'Set up home directory + permissions' },
  { key: 'openclaw_onboard',    label: 'Initialize OpenClaw' },
  { key: 'add_bot_to_network',  label: 'Register in pod network' },
  { key: 'deploy_bot',          label: 'Deploy gateway + workspace' },
];

let _wizState = null;
let _wizPreviewTimer = null;
let _wizJobPollTimer = null;

// Hide the template form-field when only "blank" is available — a
// single-option dropdown is dead UI. Call this after any code that
// adds/removes <option> elements on #wiz-template so the field
// re-appears organically once real templates ship.
function _wizUpdateTemplateVisibility() {
  const sel = document.getElementById('wiz-template');
  const field = document.getElementById('wiz-template-field');
  if (!sel || !field) return;
  const nonBlank = Array.from(sel.options).filter(o => o.value !== 'blank').length;
  field.style.display = nonBlank === 0 ? 'none' : '';
}

function openAddBotModal() {
  _wizState = {
    description: '',
    template: 'blank',
    bot_id: '',
    user: '',
    uid: null,
    port: null,
    role: 'member',
    allow_existing_user: false,
    suggested_bot_id: '',
    job_id: null,
    job_result: null,
    last_preview: null,
    mode: 'add-bot',  // or 'install-evo' (see openInstallEvoWizard)
  };
  // Reset form fields
  document.getElementById('wiz-description').value = '';
  document.getElementById('wiz-template').value = 'blank';
  _wizUpdateTemplateVisibility();
  document.getElementById('wiz-bot-id').value = '';
  document.getElementById('wiz-user').value = '';
  document.getElementById('wiz-uid').value = '';
  document.getElementById('wiz-port').value = '';
  document.getElementById('wiz-role').value = 'member';
  document.getElementById('wiz-allow-existing-user').checked = false;
  document.getElementById('wiz-s2-issues').style.display = 'none';
  document.getElementById('wiz-s2-suggestion').style.display = 'none';
  document.getElementById('wiz-s3-stages').innerHTML = '';
  document.getElementById('wiz-s3-log').innerHTML = '';
  document.getElementById('wiz-s3-failure').style.display = 'none';
  document.getElementById('wiz-s3-next').style.display = 'none';
  document.getElementById('wiz-s3-cancel').style.display = 'none';
  document.getElementById('wiz-s4-error').style.display = 'none';
  document.getElementById('wiz-s4-channel-detail').style.display = 'none';
  document.getElementById('wiz-purpose-archetype').value = '';
  document.getElementById('wiz-purpose-mission').value = '';
  document.querySelectorAll('input[name="wiz-msg-channel"]').forEach(r => r.checked = r.value === 'none');
  _wizApplyMode();
  _wizGoto(1);
  document.getElementById('add-bot-modal').classList.add('open');
}

// Conversational entry point (spec-add-bot-wizard-build-delta-2026-06-10
// §2.1 Option C): new bots are created in conversation with the main
// bot. Close the modal, jump to the Home chat, and pre-fill
// `evo add-bot` so one Enter starts the conversation.
function _wizOpenConversationalAdd() {
  closeAddBotModal();
  const navEl = document.querySelector('.nav-item[data-page="home"]');
  if (navEl && typeof nav === 'function') nav(navEl);
  const input = document.getElementById('home-prompt-input');
  if (input) {
    input.value = 'evo add-bot';
    if (typeof autoResizeComposer === 'function') autoResizeComposer(input);
    input.focus();
  }
}

// Install-evo mode (PR follow-up to #1890). Entered from the
// setup-incomplete banner when /api/setup-status reports no primary.
// Locks bot_id/role/user to the canonical install-primary values that
// setup_wizard.py uses for fresh installs.
function openInstallEvoWizard() {
  openAddBotModal();
  _wizState.mode = 'install-evo';
  _wizState.bot_id = 'evo';
  _wizState.user = 'evo';
  _wizState.role = 'primary';
  document.getElementById('wiz-bot-id').value = 'evo';
  document.getElementById('wiz-user').value = 'evo';
  document.getElementById('wiz-role').value = 'primary';
  document.getElementById('wiz-description').value = 'Evolve primary bot — conversational + alerts partner.';
  _wizApplyMode();
}

// Toggle locked-mode affordances on the form based on _wizState.mode.
// Idempotent — safe to call after every state change.
function _wizApplyMode() {
  const installEvo = _wizState && _wizState.mode === 'install-evo';
  const header = document.getElementById('wiz-install-evo-header');
  if (header) header.style.display = installEvo ? '' : 'none';
  // The "ask your main bot" entry point makes no sense while installing
  // the main bot itself.
  const convEntry = document.getElementById('wiz-conversational-entry');
  if (convEntry) convEntry.style.display = installEvo ? 'none' : '';
  const s1Title = document.getElementById('wiz-s1-title');
  const s1Sub = document.getElementById('wiz-s1-subtitle');
  if (s1Title) s1Title.textContent = installEvo ? 'Complete Evolve setup' : 'Add a bot';
  if (s1Sub) s1Sub.textContent = installEvo
    ? "Installing the evo primary bot. The next screen confirms the locked identity — there's no choice to make here."
    : 'What do you want this bot to do? A short description helps us suggest a sensible name and defaults.';
  const s2Title = document.getElementById('wiz-s2-title');
  if (s2Title) s2Title.textContent = installEvo ? 'Confirm evo identity' : 'Bot identity';
  const s2Sub = document.getElementById('wiz-s2-subtitle');
  if (s2Sub) s2Sub.textContent = installEvo
    ? "Bot ID, macOS account, and role are fixed for the primary install. Port + UID auto-allocate."
    : 'Pick a short identifier and confirm port allocation. Everything else has a sensible default.';
  // Lock / unlock the form controls. The hidden role input has its
  // value forced; visible inputs become disabled so the operator can't
  // edit them but can still read what's locked in.
  const botIdInput = document.getElementById('wiz-bot-id');
  const userInput = document.getElementById('wiz-user');
  const allowExisting = document.getElementById('wiz-allow-existing-user');
  if (botIdInput) botIdInput.disabled = !!installEvo;
  if (userInput) userInput.disabled = !!installEvo;
  if (allowExisting) allowExisting.disabled = !!installEvo;
  const helper = document.getElementById('wiz-bot-id-helper');
  if (helper) helper.style.display = installEvo ? '' : 'none';
  // Cancel buttons stay clickable (operator can close the modal), but
  // the banner persists until install completes. Per-spec: "Cancel/Skip
  // removed (operator can close the modal but the banner persists)."
  // We keep them visually present so the modal isn't a trap, but the
  // banner remains the source of truth.
}

function closeAddBotModal() {
  if (_wizJobPollTimer) { clearTimeout(_wizJobPollTimer); _wizJobPollTimer = null; }
  if (_wizPreviewTimer) { clearTimeout(_wizPreviewTimer); _wizPreviewTimer = null; }
  // Stop the verification gauntlet pollers. The old e2e contract
  // disarm-on-close call was retired 2026-06-01 along with Check 4 —
  // no POD_CONDUCT block to clean up anymore.
  if (_verifyState?.pollTimer)     { clearTimeout(_verifyState.pollTimer); }
  _verifyState = { jobId: null, pollTimer: null, result: null };
  document.getElementById('add-bot-modal').classList.remove('open');
  // If the bot was provisioned, refresh the overview pod tiles so the
  // new bot shows up immediately. Also re-check setup-status — if this
  // was the install-evo flow, the banner needs to disappear.
  if (_wizState && _wizState.job_result && _wizState.job_result.ok) {
    try { loadStatus(); } catch (e) { /* non-fatal */ }
    try { refreshSetupStatus(); } catch (e) { /* non-fatal */ }
  }
}

function _wizGoto(screen) {
  for (let i = 1; i <= 5; i++) {
    const el = document.getElementById('wiz-screen-' + i);
    const ind = document.getElementById('wiz-ind-' + i);
    if (el) el.style.display = (i === screen) ? '' : 'none';
    if (ind) {
      ind.style.fontWeight = (i === screen) ? '600' : '400';
      ind.style.color = (i === screen) ? 'var(--accent)' :
                        (i < screen) ? 'var(--text2)' : 'var(--text3)';
    }
  }
}

// ── Screen 1 → 2 ──────────────────────────────────────────────────────
async function _wizScreen1Next() {
  const desc = document.getElementById('wiz-description').value.trim();
  const tmpl = document.getElementById('wiz-template').value;
  _wizState.description = desc;
  _wizState.template = tmpl;
  const installEvo = _wizState.mode === 'install-evo';
  // Fetch a preview (no commit) — sets the suggested bot_id +
  // resolved UID/port defaults for Screen 2 to display.
  // In install-evo mode the name is locked; we still call preview for
  // its port/uid allocation hints but we DON'T overwrite the bot_id.
  const btn = document.getElementById('wiz-s1-next');
  btn.disabled = true;
  btn.textContent = 'Thinking…';
  try {
    const r = await api('POST', '/api/wizard/preview', installEvo
      ? { bot_id: 'evo', user: 'evo', role: 'primary' }
      : { description: desc || 'newbot', role: 'member' });
    if (installEvo) {
      if (r && r.resolved && r.resolved.uid > 0) {
        document.getElementById('wiz-uid').placeholder = 'auto (' + r.resolved.uid + ')';
      }
      if (r && r.resolved && r.resolved.port > 0) {
        document.getElementById('wiz-port').value = r.resolved.port;
      }
    } else if (r && r.suggested_bot_id) {
      _wizState.suggested_bot_id = r.suggested_bot_id;
      document.getElementById('wiz-bot-id').value = r.suggested_bot_id;
      if (r.resolved && r.resolved.uid > 0) {
        document.getElementById('wiz-uid').placeholder = 'auto (' + r.resolved.uid + ')';
      }
      if (r.resolved && r.resolved.port > 0) {
        document.getElementById('wiz-port').value = r.resolved.port;
      }
      const sugEl = document.getElementById('wiz-s2-suggestion');
      sugEl.innerHTML = 'Suggested name from your description: <b>' + escHtml(r.suggested_bot_id) + '</b> (change it if you prefer)';
      sugEl.style.display = '';
    }
  } catch (e) {
    // Preview failure is non-fatal — operator can type any bot_id
    console.warn('wizard preview failed', e);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Continue →';
  }
  _wizGoto(2);
  // Cost-to-first-value: project + show the new bot's cost envelope on the
  // commit screen so the operator accepts it before provisioning. Fire-and-
  // forget — purely informational, never blocks the wizard.
  _wizFetchCostProjection();
  if (!installEvo) {
    document.getElementById('wiz-bot-id').focus();
  }
}

// ── Screen 2: cost-to-first-value panel ───────────────────────────────
// Projected cost envelope for the new bot, read from the real cap
// resolvers via /api/wizard/cost-projection (the graduated daily cap +
// the one-time provisioning ceiling). No dollar literals live here — the
// figures the operator accepts are the figures the enforcers apply.
function _wizFmtUsd(n) {
  if (n === null || n === undefined || isNaN(n)) return null;
  return Number.isInteger(n) ? '$' + n : '$' + Number(n).toFixed(2);
}

async function _wizFetchCostProjection() {
  try {
    const r = await api('GET', '/api/wizard/cost-projection');
    _wizRenderCostPanel(r || {});
  } catch (e) {
    // Non-fatal — the panel is informational; hide it on failure rather
    // than block provisioning.
    console.warn('wizard cost-projection failed', e);
    const panel = document.getElementById('wiz-s2-cost-panel');
    if (panel) panel.style.display = 'none';
  }
}

function _wizRenderCostPanel(data) {
  const panel = document.getElementById('wiz-s2-cost-panel');
  const line = document.getElementById('wiz-s2-cost-line');
  if (!panel || !line) return;
  const ceiling = _wizFmtUsd(data.provisioning_ceiling_usd);
  const hard = _wizFmtUsd(data.daily_hard_cap_usd);
  const hardAfter = _wizFmtUsd(data.daily_hard_cap_after_window_usd);
  const alert = _wizFmtUsd(data.daily_alert_usd);
  const days = Number(data.window_days) || 7;
  const parts = [];
  if (ceiling) parts.push('≈' + ceiling + ' one-time provisioning');
  if (alert) parts.push('alert ' + alert + '/day');
  if (hard) {
    let stop = 'hard stop ' + hard + '/day';
    if (hardAfter && hardAfter !== hard) {
      stop += ' (first ' + days + ' days, then ' + hardAfter + '/day)';
    }
    parts.push(stop);
  }
  if (!parts.length) { panel.style.display = 'none'; return; }
  // textContent (not innerHTML): all values are server-resolved numbers,
  // no markup, no escaping concern.
  line.textContent = parts.join(' · ');
  panel.style.display = '';
}

// ── Screen 2: live validation ─────────────────────────────────────────
function _wizDebouncedPreview() {
  if (_wizPreviewTimer) clearTimeout(_wizPreviewTimer);
  _wizPreviewTimer = setTimeout(_wizRunPreview, 400);
}

async function _wizRunPreview() {
  const body = {
    description: _wizState.description || _wizState.suggested_bot_id || 'x',
    bot_id: document.getElementById('wiz-bot-id').value.trim(),
    user: document.getElementById('wiz-user').value.trim() || null,
    port: parseInt(document.getElementById('wiz-port').value) || null,
    uid: parseInt(document.getElementById('wiz-uid').value) || null,
    role: document.getElementById('wiz-role').value,
    allow_existing_user: document.getElementById('wiz-allow-existing-user').checked,
  };
  const r = await api('POST', '/api/wizard/preview', body);
  _wizState.last_preview = r;
  const issuesEl = document.getElementById('wiz-s2-issues');
  const nextBtn = document.getElementById('wiz-s2-next');
  if (!r || !r.validation) {
    issuesEl.style.display = 'none';
    nextBtn.disabled = false;
    return;
  }
  const issues = r.validation.issues || [];
  if (issues.length === 0) {
    issuesEl.style.display = 'none';
    nextBtn.disabled = false;
  } else {
    issuesEl.innerHTML = issues.map(i =>
      `<div style="background:rgba(248,113,113,0.1);border:1px solid rgba(248,113,113,0.3);border-radius:6px;padding:6px 10px;font-size:0.78rem;color:var(--red);margin-bottom:4px">⚠ <b>${escHtml(i.field)}</b>: ${escHtml(i.message)}</div>`
    ).join('');
    issuesEl.style.display = '';
    nextBtn.disabled = true;
  }
}

// ── Screen 2: LLM provider chooser ────────────────────────────────────
//
// Each entry mirrors one of OpenClaw's documented `--auth-choice` values
// (see `openclaw onboard --help`). `key_label` / `key_placeholder` /
// `key_prefix` drive the live key-input affordance; `key_required` is
// false for local providers where OC discovers the daemon on its own.
// `custom: true` reveals the base-url/compat/provider-id/model-id block.
// ── Screen 2 → 3: kick off provision ──────────────────────────────────
// Provider-aware metadata for the LLM key field. Each entry: the
// human label, the input placeholder, an optional `prefix` for
// client-side shape validation, an optional `keyless: true` for
// local providers (Ollama / LM Studio) that have no API key, and
// `custom: true` for the OpenAI-compatible escape hatch which
// reveals the base-URL / compatibility / provider-id / model-id
// sub-block. The auth_choice value is what we send to
// /api/wizard/provision — it must match an entry in
// provisioning.AUTH_CHOICE_TO_KEY_FLAG on the backend.
//
// Evolve is LLM-provider-agnostic: this list mirrors what OpenClaw's
// `--auth-choice` enum supports. Subscription / interactive auth
// (claude-cli, codex-cli) is a separate chip — those need their own UI.
const _WIZ_LLM_PROVIDERS = {
  'anthropic':          { label: 'Anthropic API key', placeholder: 'sk-ant-...',    prefix: 'sk-ant-' },
  'openai-api-key':     { label: 'OpenAI API key',    placeholder: 'sk-...',        prefix: 'sk-'     },
  'gemini-api-key':     { label: 'Gemini API key',    placeholder: 'AIza...',       prefix: 'AIza'    },
  'xai-api-key':        { label: 'xAI API key',       placeholder: 'xai-...',       prefix: 'xai-'    },
  'deepseek-api-key':   { label: 'DeepSeek API key',  placeholder: 'sk-...',        prefix: 'sk-'     },
  'groq-api-key':       { label: 'Groq API key',      placeholder: 'gsk_...',       prefix: 'gsk_'    },
  'mistral-api-key':    { label: 'Mistral API key',   placeholder: '',              prefix: ''        },
  'openrouter-api-key': { label: 'OpenRouter API key',placeholder: 'sk-or-...',     prefix: 'sk-or-'  },
  'together-api-key':   { label: 'Together AI API key', placeholder: '',            prefix: ''        },
  'fireworks-api-key':  { label: 'Fireworks API key', placeholder: '',              prefix: ''        },
  'moonshot-api-key':   { label: 'Moonshot API key',  placeholder: '',              prefix: ''        },
  'nvidia-api-key':     { label: 'NVIDIA API key',    placeholder: '',              prefix: ''        },
  // Local providers: OC discovers the daemon from the environment.
  // No API key required; we DISABLE the key field rather than hide
  // it, so the operator can see why nothing's expected of them.
  'ollama':             { label: 'Ollama (local)',    placeholder: 'no key — OC reads OLLAMA_HOST', prefix: '', keyless: true },
  'lmstudio':           { label: 'LM Studio (local)', placeholder: 'no key — OC discovers the daemon', prefix: '', keyless: true },
  // Custom OpenAI-compatible endpoint. Key is optional (local servers
  // often have no auth); the custom-extras sub-block carries the
  // base URL + compat + IDs.
  'custom-api-key':     { label: 'API key', placeholder: 'optional for local endpoints', prefix: '', custom: true },
};

// Wizard auth_choice → /api/wizard/borrow-candidates?provider=...
// short name. The candidates endpoint matches profile keys by prefix
// "<provider>:" — see borrow_candidates in wizard_routes.py.
const _WIZ_AUTH_CHOICE_TO_PROFILE_PROVIDER = {
  'anthropic':          'anthropic',
  'anthropic-api-key':  'anthropic',
  'openai-api-key':     'openai',
  'gemini-api-key':     'gemini',
  'xai-api-key':        'xai',
  'deepseek-api-key':   'deepseek',
  'groq-api-key':       'groq',
  'mistral-api-key':    'mistral',
  'openrouter-api-key': 'openrouter',
  'together-api-key':   'together',
  'fireworks-api-key':  'fireworks',
  'moonshot-api-key':   'moonshot',
  'nvidia-api-key':     'nvidia',
};

async function _wizUpdateLlmField() {
  const sel = document.getElementById('wiz-llm-provider');
  const srcWrap = document.getElementById('wiz-llm-cred-source-wrap');
  const srcLabel = document.getElementById('wiz-llm-cred-source-label');
  const options = document.getElementById('wiz-llm-cred-options');
  const pasteWrap = document.getElementById('wiz-llm-paste-wrap');
  const input = document.getElementById('wiz-llm-api-key');
  const hint = document.getElementById('wiz-llm-cred-hint');
  const customWrap = document.getElementById('wiz-llm-custom-wrap');
  if (!sel.value) {
    // No provider chosen — Evolve never presumes a provider; hide the
    // credential-source UI until the operator picks one.
    srcWrap.style.display = 'none';
    hint.style.display = '';
    hint.textContent = 'Pick a provider above — the credential source will appear here.';
    input.value = '';
    customWrap.style.display = 'none';
    return;
  }
  const meta = _WIZ_LLM_PROVIDERS[sel.value] || { label: 'API key', placeholder: '', prefix: '' };
  customWrap.style.display = meta.custom ? '' : 'none';

  // Local providers (ollama, lmstudio) don't take a key at all — OC
  // discovers the daemon from the environment. Surface that clearly
  // instead of asking for credentials.
  if (meta.keyless) {
    srcWrap.style.display = 'none';
    hint.style.display = '';
    hint.textContent = `${meta.label} — no API key required. OpenClaw discovers the daemon from the environment.`;
    input.value = '';
    return;
  }

  // Cloud provider — populate the credential source list with
  // borrow-from-bot options + a paste-new option. Operator must pick.
  hint.style.display = 'none';
  srcWrap.style.display = '';
  srcLabel.firstChild.nodeValue = meta.label + ' source ';
  options.innerHTML = '<div style="color:var(--text3);font-size:0.78rem">Looking for sibling bots with a ' + meta.label + '…</div>';
  pasteWrap.style.display = 'none';
  input.placeholder = meta.placeholder || 'paste your key';

  // Ask the backend which bots have a key for this provider. The
  // provider name sent to the candidates endpoint is the profile-
  // prefix, NOT the auth_choice — see _WIZ_AUTH_CHOICE_TO_PROFILE_
  // PROVIDER for the mapping.
  //
  // Response shape: {provider, bots: [{bot_id, profile_keys}]} — read
  // r.bots (the original Screen 4 code's contract). The earlier
  // refactor that moved this to Screen 2 incorrectly read r.candidates,
  // which silently collapsed to [] every time and rendered only the
  // Paste radio. See wizard_routes.api_wizard_borrow_candidates.
  const profileProvider = _WIZ_AUTH_CHOICE_TO_PROFILE_PROVIDER[sel.value];
  let candidates = [];
  if (profileProvider) {
    try {
      const r = await api('GET', `/api/wizard/borrow-candidates?provider=${encodeURIComponent(profileProvider)}`);
      candidates = (r && Array.isArray(r.bots)) ? r.bots : [];
    } catch (e) {
      // Borrow lookup failed — fall through to paste-only.
      candidates = [];
    }
  }

  // Render the radio list. No default selection — operator must pick.
  const lines = [];
  candidates.forEach(c => {
    lines.push(`
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
        <input type="radio" name="wiz-llm-cred-source" value="borrow:${escHtml(c.bot_id)}" onchange="_wizLlmSourceChanged()">
        <span>Borrow from <strong>${escHtml(botLabel(c.bot_id))}</strong></span>
      </label>
    `);
  });
  // Always offer paste-new as the universal fallback. Custom providers
  // also use this path (their key is optional, validated on submit).
  lines.push(`
    <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
      <input type="radio" name="wiz-llm-cred-source" value="paste" onchange="_wizLlmSourceChanged()">
      <span>Paste a new ${escHtml(meta.label)}</span>
    </label>
  `);
  options.innerHTML = lines.join('');
}

function _wizLlmSourceChanged() {
  // Reveal the paste input only when the operator picks "Paste new".
  const checked = document.querySelector('input[name="wiz-llm-cred-source"]:checked');
  const pasteWrap = document.getElementById('wiz-llm-paste-wrap');
  if (checked && checked.value === 'paste') {
    pasteWrap.style.display = '';
    document.getElementById('wiz-llm-api-key').focus();
  } else {
    pasteWrap.style.display = 'none';
    document.getElementById('wiz-llm-api-key').value = '';
  }
}

async function _wizScreen2Next() {
  const installEvo = _wizState.mode === 'install-evo';
  // Pull final values into state. Install-evo mode forces the locked
  // identity regardless of any DOM state — the disabled inputs are an
  // affordance, not the source of truth.
  if (installEvo) {
    _wizState.bot_id = 'evo';
    _wizState.user = 'evo';
    _wizState.role = 'primary';
    _wizState.allow_existing_user = false;
  } else {
    _wizState.bot_id = document.getElementById('wiz-bot-id').value.trim();
    _wizState.user = document.getElementById('wiz-user').value.trim() || null;
    _wizState.role = document.getElementById('wiz-role').value;
    _wizState.allow_existing_user = document.getElementById('wiz-allow-existing-user').checked;
  }
  _wizState.port = parseInt(document.getElementById('wiz-port').value) || null;
  _wizState.uid = parseInt(document.getElementById('wiz-uid').value) || null;
  _wizState.auth_choice = document.getElementById('wiz-llm-provider').value;
  if (!_wizState.bot_id) { toast('Bot ID is required', 'err'); return; }
  // The provider chooser has NO default selection — Evolve never
  // presumes Anthropic or any other provider. Operator must pick.
  if (!_wizState.auth_choice) {
    toast('Pick an LLM provider for the bot — Evolve supports many; none are presumed', 'err');
    return;
  }
  // LLM credential validation. Three shapes:
  //   - Cloud provider: operator picked Borrow-from-<bot> OR Paste-new
  //   - Local provider (ollama, lmstudio): no key, no check
  //   - Custom: optional key (local endpoints often need none)
  const providerMeta = _WIZ_LLM_PROVIDERS[_wizState.auth_choice] || {};
  _wizState.provider_api_key = null;
  _wizState.borrow_provider_from_bot = null;
  if (providerMeta.keyless) {
    // ollama / lmstudio — no credential at all.
  } else {
    // Cloud or custom: a credential source must be chosen (or "Paste
    // new" with the optional field for custom).
    const checked = document.querySelector('input[name="wiz-llm-cred-source"]:checked');
    if (!checked && !providerMeta.custom) {
      toast(`Pick a credential source for ${providerMeta.label || 'this provider'} — borrow from another bot or paste a new key`, 'err');
      return;
    }
    if (checked && checked.value.startsWith('borrow:')) {
      _wizState.borrow_provider_from_bot = checked.value.substring('borrow:'.length);
    } else {
      // Paste path (either explicitly chosen, or implicit for custom
      // providers without a borrow source picked).
      _wizState.provider_api_key = document.getElementById('wiz-llm-api-key').value.trim();
      if (!providerMeta.custom && !_wizState.provider_api_key) {
        toast(`${providerMeta.label || 'API key'} is required — OpenClaw needs it to start`, 'err');
        return;
      }
      if (providerMeta.prefix && _wizState.provider_api_key &&
          !_wizState.provider_api_key.startsWith(providerMeta.prefix)) {
        toast(`${providerMeta.label} should start with "${providerMeta.prefix}"`, 'err');
        return;
      }
    }
  }

  // Custom-provider extras — required pair is base URL + compatibility.
  // Provider-id + model-id are optional (OC auto-derives the provider-id
  // from the URL and operators often want the catalog to default empty).
  if (_wizState.auth_choice === 'custom-api-key') {
    _wizState.custom_base_url = document.getElementById('wiz-llm-custom-base-url').value.trim();
    _wizState.custom_compatibility = document.getElementById('wiz-llm-custom-compat').value;
    _wizState.custom_provider_id = document.getElementById('wiz-llm-custom-provider-id').value.trim();
    _wizState.custom_model_id = document.getElementById('wiz-llm-custom-model-id').value.trim();
    if (!_wizState.custom_base_url) {
      toast('Custom provider needs a base URL', 'err');
      return;
    }
  } else {
    _wizState.custom_base_url = null;
    _wizState.custom_compatibility = null;
    _wizState.custom_provider_id = null;
    _wizState.custom_model_id = null;
  }

  document.getElementById('wiz-s3-name').textContent = _wizState.bot_id;
  _wizRenderStages('pending');
  _wizGoto(3);

  // Kick off provision job. Credentials reach the backend as ONE OF:
  //   - ``provider_api_key`` (operator pasted the key directly)
  //   - ``borrow_provider_from_bot`` (operator picked a sibling bot;
  //     wizard_routes.py reads the key from that bot's auth-profiles
  //     and resolves to a raw key server-side — frontend never sees it)
  // provision_bot then dispatches via AUTH_CHOICE_TO_KEY_FLAG to the
  // right ``--<provider>-api-key`` flag on the openclaw onboard
  // command line. The ``custom_*`` fields are only consulted when
  // ``auth_choice == "custom-api-key"``.
  const body = {
    bot_id: _wizState.bot_id,
    user: _wizState.user,
    uid: _wizState.uid,
    port: _wizState.port,
    role: _wizState.role,
    allow_existing_user: _wizState.allow_existing_user,
    auth_choice: _wizState.auth_choice,
    provider_api_key: _wizState.provider_api_key,
    borrow_provider_from_bot: _wizState.borrow_provider_from_bot,
    custom_base_url: _wizState.custom_base_url,
    custom_compatibility: _wizState.custom_compatibility,
    custom_provider_id: _wizState.custom_provider_id,
    custom_model_id: _wizState.custom_model_id,
  };
  const r = await api('POST', '/api/wizard/provision', body);
  if (!r || !r.jobId) {
    _wizShowFailure('Could not start provision: ' + (r?.error || 'unknown error'));
    return;
  }
  _wizState.job_id = r.jobId;
  _wizPollJob();
}

function _wizRenderStages(initialStatus) {
  // initialStatus: 'pending' for all stages, or a map of {stage_key: status}
  const el = document.getElementById('wiz-s3-stages');
  el.innerHTML = _WIZ_STAGES.map(s => {
    const status = (typeof initialStatus === 'object' ? initialStatus[s.key] : initialStatus) || 'pending';
    let icon = '<span style="color:var(--text3)">○</span>';
    let lblColor = 'var(--text2)';
    if (status === 'start')   { icon = '<span style="color:var(--accent)">⟳</span>'; lblColor = 'var(--text)'; }
    if (status === 'ok')      { icon = '<span style="color:var(--green)">✓</span>';  lblColor = 'var(--text)'; }
    if (status === 'skip')    { icon = '<span style="color:var(--text3)">↷</span>'; }
    if (status === 'fail')    { icon = '<span style="color:var(--red)">✗</span>';    lblColor = 'var(--red)'; }
    return `<div data-stage="${s.key}" style="display:flex;gap:8px;align-items:center"><div style="width:18px;text-align:center">${icon}</div><div style="color:${lblColor}">${escHtml(s.label)}</div></div>`;
  }).join('');
}

async function _wizPollJob() {
  const job = await api('GET', '/api/jobs/' + encodeURIComponent(_wizState.job_id));
  if (!job) {
    _wizShowFailure('Lost track of provision job. Check the admin server logs.');
    return;
  }
  // Re-render stages from the log entries
  const stageStatus = {};
  (job.log || []).forEach(entry => {
    const m = (entry.msg || '').match(/^([a-z_]+): (start|ok|skip|fail)(?:: (.+))?$/);
    if (m) stageStatus[m[1]] = m[2];
  });
  _wizRenderStages(stageStatus);
  // Append new log lines
  const logEl = document.getElementById('wiz-s3-log');
  logEl.innerHTML = (job.log || []).map(entry => {
    const color = entry.level === 'error' ? 'var(--red)'
                : entry.level === 'success' ? 'var(--green)'
                : entry.level === 'warning' ? 'var(--yellow)'
                : 'var(--text2)';
    return `<div style="color:${color}"><span style="color:var(--text3)">${escHtml(entry.t || '')}</span> ${escHtml(entry.msg || '')}</div>`;
  }).join('');
  logEl.scrollTop = logEl.scrollHeight;

  if (job.status === 'complete') {
    _wizState.job_result = job.result || {};
    if (_wizState.job_result.ok) {
      document.getElementById('wiz-s3-next').style.display = '';
      document.getElementById('wiz-s3-cancel').style.display = '';
    } else {
      _wizShowFailure(_wizState.job_result.error || 'Provision failed; check the live log.');
    }
    return;
  }
  if (job.status === 'failed') {
    _wizShowFailure(job.error || 'Provision job failed.');
    return;
  }
  // Still running — poll again
  _wizJobPollTimer = setTimeout(_wizPollJob, 1200);
}

function _wizShowFailure(msg) {
  const el = document.getElementById('wiz-s3-failure');
  el.innerHTML = '⚠ ' + escHtml(msg) + '<div style="color:var(--text2);margin-top:6px;font-size:0.76rem">Rollback (if any) is shown in the live log above. Close this dialog and inspect the admin logs if you need more detail.</div>';
  el.style.display = '';
  document.getElementById('wiz-s3-cancel').style.display = '';
  document.getElementById('wiz-s3-cancel').textContent = 'Close';
}

// ── Screen 4 setup: channel picker only ───────────────────────────────
// LLM credential is collected on Screen 2 (it's required before
// openclaw onboard runs). Screen 4 used to re-prompt with a
// default-checked "Borrow from <first sibling>" radio that silently
// fired /api/credentials/borrow on Apply — joining a sibling's key
// into the new bot's auth-profiles regardless of what the operator
// picked on Screen 2. Removed; Screen 4 is channel-only now.

function _wizChannelChanged() {
  const sel = document.querySelector('input[name="wiz-msg-channel"]:checked');
  const detail = document.getElementById('wiz-s4-channel-detail');
  if (!sel || sel.value === 'none') {
    detail.style.display = 'none';
    return;
  }
  // Each channel gets a minimal inline detail. The wizard collects the
  // token and calls the existing per-skill install endpoint when "Apply
  // & continue" runs — reusing the dialog logic from PRs #1679/#1681
  // would massively bloat this modal, so we keep it spare here.
  let body = '';
  if (sel.value === 'telegram') {
    body = `
      <div style="font-size:0.78rem;color:var(--text2);margin-bottom:8px">
        Paste the bot token from <a href="https://t.me/BotFather" target="_blank" style="color:var(--accent)">@BotFather</a>.
        After install, you'll likely want to <b>disable BotFather's privacy mode</b> so the bot can read group messages
        (<code style="font-size:0.74rem">/setprivacy → Disable</code>).
      </div>
      <input id="wiz-s4-tg-token" placeholder="123456:ABC-DEF..." style="font-family:ui-monospace,Menlo,monospace;font-size:0.78rem;width:100%">`;
  } else if (sel.value === 'slack') {
    // Slack-only OAuth-pending pattern. Discord moved to inline
    // bot-token-direct in PR #1911 (mirrors Telegram); Slack stays
    // OAuth-only because there's no bot-token-direct backend path
    // for it (see /api/skills/install/slack/start-oauth + /poll +
    // /confirm). The wizard stashes pending_oauth_channel and
    // Done's _wizScreen5Done() routes to the bot's Skills page
    // where the OAuth Connect button lives.
    //
    // Pre-2026-06-01 the Slack branch promised a deep-link
    // ("the wizard will deep-link you after provisioning") but did
    // NOTHING on Apply — operator landed on Verify with no Slack
    // installed and no instructions about where the deep-link went.
    body = `
      <div style="font-size:0.78rem;color:var(--text2);margin-bottom:8px">
        Slack uses OAuth — no token to paste here. When you click
        <b>Apply &amp; continue</b>, we'll finish provisioning and
        route you to the bot's Skills page on Done to complete the
        Slack OAuth handshake.
      </div>
      <div style="font-size:0.76rem;color:var(--text3)">
        The Verify screen will show a Slack-OAuth-pending banner —
        that's expected; the Skills page picks up from there.
      </div>`;
  } else if (sel.value === 'discord') {
    body = `
      <div style="font-size:0.78rem;color:var(--text2);margin-bottom:8px">
        Paste the Discord bot token from the <a href="https://discord.com/developers/applications" target="_blank" style="color:var(--accent)">Discord developer portal</a>.
      </div>
      <input id="wiz-s4-discord-token" placeholder="bot token..." style="font-family:ui-monospace,Menlo,monospace;font-size:0.78rem;width:100%">`;
  }
  detail.innerHTML = body;
  detail.style.display = '';
}

// Derive the review-summary label for the LLM credential from what
// the operator actually picked on Screen 2 — borrow source, pasted
// key, or neither (keyless / skip). The wizard never re-prompts for
// an LLM credential on Screen 4.
function _wizDeriveAppliedLlmSource() {
  if (_wizState.borrow_provider_from_bot) {
    return 'borrow:' + _wizState.borrow_provider_from_bot;
  }
  if (_wizState.provider_api_key) {
    return 'paste';
  }
  return 'skip';
}

// ── Screen 4: optional bot purpose capture ────────────────────────────
// Purpose (archetype + one-line mission) is the Effectiveness-Layer anchor
// the Fit Reviewer reads to know what "more effective" means for this bot
// (network.json::bots[<id>].purpose). It's OPTIONAL on this screen: the bot
// already exists (committed at the end of Screen 3), so we just PUT the
// declaration to the same endpoint the per-bot Purpose card uses. A blank
// archetype AND blank mission means the operator skipped — we don't POST,
// and the per-bot Setup checklist's "purpose" item stays pending to nag
// them later. Fire-and-forget on purpose: a slow or failed save must never
// block the wizard's progression to Verify (the checklist is the backstop).
async function _wizSavePurpose() {
  const botId = _wizState && _wizState.bot_id;
  if (!botId) return;
  const archEl = document.getElementById('wiz-purpose-archetype');
  const missEl = document.getElementById('wiz-purpose-mission');
  const archetype = archEl ? archEl.value.trim() : '';
  const mission = missEl ? missEl.value.trim() : '';
  // Both blank → operator skipped purpose. Don't POST (an empty payload
  // just clears, a no-op on a fresh bot). The checklist nags instead.
  if (!archetype && !mission) return;
  try {
    await api('PUT', `/api/bot/${encodeURIComponent(botId)}/purpose`,
              { archetype, mission });
  } catch (e) {
    // Non-fatal — if this didn't land, the Setup checklist's "purpose"
    // item stays pending and the operator is prompted again.
    console.warn('wizard purpose save failed', e);
  }
}

// ── Screen 4 → 5: skip path (no channel apply) ────────────────────────
// Used by the "Skip →" button on Screen 4. We still want to enter the
// Verify gauntlet so the operator sees what state the bot is actually
// in — skipping the channel leaves Check 3 red ("no messaging channel
// installed"), which is exactly what we want to surface. "Skip" skips the
// messaging channel, NOT the purpose — if the operator filled in the
// optional goals/purpose fields we still persist them here.
function _wizScreen4Skip() {
  _wizSavePurpose();  // fire-and-forget; never blocks the screen transition
  _wizState.applied_llm_source = _wizDeriveAppliedLlmSource();
  _wizState.applied_channel = _wizState.applied_channel || 'none';
  _wizRenderReviewSummary();
  _wizGoto(5);
  _wizVerifyStart();
}

// ── Screen 4 → 5: apply messaging channel ─────────────────────────────
//
// LLM credentials are set on Screen 2 (operator picked paste-new or
// borrow-from-<bot>, plumbed through /api/wizard/provision into the
// openclaw onboard CLI). Do NOT prompt for or apply LLM credentials
// here — see the comment block above the deleted helpers earlier in
// this file for the silent-auto-borrow incident this guard prevents.
async function _wizScreen4Apply() {
  const errEl = document.getElementById('wiz-s4-error');
  errEl.style.display = 'none';
  const btn = document.getElementById('wiz-s4-apply');
  btn.disabled = true;
  btn.textContent = 'Applying…';

  const channelSel = document.querySelector('input[name="wiz-msg-channel"]:checked');
  const channel = channelSel ? channelSel.value : 'none';

  const errors = [];

  // Reset any prior pending-OAuth flag — re-running Apply with a
  // different channel should overwrite, not accumulate.
  _wizState.pending_oauth_channel = null;
  if (channel === 'telegram') {
    const tok = (document.getElementById('wiz-s4-tg-token')?.value || '').trim();
    if (!tok) {
      errors.push('Telegram bot token is required if you pick Telegram.');
    } else {
      // Endpoint contract (server.py:19869): {bot_id, bot_token}.
      // Pre-2026-06-01 the wizard sent {token: ...}, which the backend
      // silently ignored — every Apply & continue with Telegram returned
      // "bot_token required" even when the operator had pasted the
      // token. Same wire-mismatch shape as the lifecycle /api/remove bug
      // closed by PR #1903.
      const r = await api('POST', '/api/skills/install/telegram/set-token',
                          { bot_id: _wizState.bot_id, bot_token: tok });
      if (!r || r.error) {
        errors.push('Telegram install failed: ' + (r?.error || r?.detail || 'unknown'));
      } else {
        // Capture the Telegram bot username so the post-install
        // pairing wizard's "Open in Telegram" button can build a
        // proper t.me/<bot>?start=... deeplink. Server returns it
        // via the embedded status.to_dict() in api_skills_telegram_set_token.
        const u = r.bot_username || (r.status && r.status.bot_username);
        if (u) _wizState.telegram_bot_username = u;
      }
    }
  } else if (channel === 'discord') {
    const tok = (document.getElementById('wiz-s4-discord-token')?.value || '').trim();
    if (!tok) {
      errors.push('Discord bot token is required if you pick Discord.');
    } else {
      // Parallel to Telegram: {bot_id, bot_token} verified via
      // /users/@me, then written + openclaw.json wired + gateway
      // kickstarted. See api_skills_discord_set_token (added in
      // PR #1911 — same shape as Telegram's set-token).
      const r = await api('POST', '/api/skills/install/discord/set-token',
                          { bot_id: _wizState.bot_id, bot_token: tok });
      if (!r || r.error) errors.push('Discord install failed: ' + (r?.error || r?.detail || 'unknown'));
    }
  } else if (channel === 'slack') {
    // Slack-only: still OAuth-based on the backend (no bot-token-direct
    // path — see /api/skills/install/slack/start-oauth + /poll +
    // /confirm). Stash the channel so Verify can surface a yellow
    // "OAuth pending" banner and Done's _wizScreen5Done() handler can
    // route to the bot's Skills page where the OAuth flow lives.
    //
    // Pre-2026-06-01 the Screen 4 Slack branch did NOTHING on Apply —
    // operator landed on Verify with no Slack installed and the copy's
    // promised deep-link never materialized. Discord originally
    // suffered the same silent-no-op, but PR #1911 added a bot-token-
    // direct backend so the Discord branch above now works inline.
    _wizState.pending_oauth_channel = 'slack';
  }

  btn.disabled = false;
  btn.textContent = 'Apply & continue →';

  if (errors.length > 0) {
    errEl.innerHTML = errors.map(e => '⚠ ' + escHtml(e)).join('<br>');
    errEl.style.display = '';
    return;
  }

  // Persist the optional goals/purpose fields (no-op if the operator left
  // them blank). Fire-and-forget — must not block the move to Verify.
  _wizSavePurpose();

  // Save the channel choice + LLM choice for the review screen. The
  // LLM source is derived from Screen 2 state — Screen 4 doesn't
  // touch LLM credentials.
  _wizState.applied_llm_source = _wizDeriveAppliedLlmSource();
  _wizState.applied_channel = channel;
  _wizRenderReviewSummary();
  _wizGoto(5);
  // Kick off the verification gauntlet (Screen 5 is now Verify, not Review).
  // The summary moves into the "All set" block that appears AFTER the
  // gauntlet completes — see _wizVerifyMaybeShowSummary.
  _wizVerifyStart();
}

function _wizGotoApplicationsPage() {
  const navEl = document.querySelector('.nav-item[data-page="capabilities"]');
  if (navEl && typeof nav === 'function') {
    nav(navEl);
  }
}

// Deep-link from the wizard to the bot's Skills page, optionally
// flagging the channel whose OAuth handshake still needs completing.
// Mirrors _wizGotoApplicationsPage above. Stashes the requested channel
// on a window-level handoff slot so skillsPageActivate() / the page's
// init code can highlight the right row + scroll it into view.
//
// The Skills page itself is the single source of truth for the OAuth
// flow (start-oauth + poll + confirm) — we don't reimplement it here.
// This helper is just navigation + a hint.
function _wizGotoSkillsPage(channel) {
  if (channel) {
    // Pull the bot id from the wizard state so the Skills page can
    // filter to it (the Skills page is per-bot scoped by selector).
    try {
      window._wizPendingChannelHandoff = {
        bot_id: _wizState?.bot_id || null,
        channel: channel,
        ts: Date.now(),
      };
    } catch (e) {
      // Non-fatal — the page still loads; the operator just won't
      // see the row pre-highlighted.
    }
  }
  const navEl = document.querySelector('.nav-item[data-page="skills"]');
  if (navEl && typeof nav === 'function') {
    nav(navEl);
  }
}

// Wraps closeAddBotModal with the post-wizard handoff to the Skills
// page when an OAuth channel was selected but not yet completed.
// Wired to the Screen 5 Done button so picking Slack or Discord lands
// the operator one click from the OAuth Connect button on the bot's
// Skills page — rather than dumping them on the Overview with no
// indication of what comes next.
function _wizScreen5Done() {
  // Only Slack uses the OAuth handoff. Telegram and Discord both have
  // inline bot-token-direct install paths (Telegram since #1683 era,
  // Discord since PR #1911), so their Done is just "close the modal".
  const pending = _wizState?.pending_oauth_channel;
  closeAddBotModal();
  if (pending === 'slack') {
    _wizGotoSkillsPage(pending);
  }
}

function _wizRenderReviewSummary() {
  const res = _wizState.job_result || {};
  const llm = _wizState.applied_llm_source || 'skip';
  const channel = _wizState.applied_channel || 'none';
  let llmLabel = 'Not set yet (bot is dormant)';
  if (llm.startsWith('borrow:')) llmLabel = 'Borrowed from <b>' + escHtml(llm.slice('borrow:'.length)) + '</b>';
  else if (llm === 'paste') llmLabel = 'Pasted manually';
  const channelLabel = channel === 'none' ? 'Not yet — operator can set up later'
                                          : escHtml(channel).charAt(0).toUpperCase() + escHtml(channel).slice(1);

  // OAuth-pending banner: when the operator picked Slack on Screen 4,
  // the wizard stashed pending_oauth_channel without completing OAuth
  // (Slack is OAuth-only on the backend). Surface that clearly here so
  // Done feels like "go finish OAuth," not "you're all set." The Done
  // button's _wizScreen5Done() handler routes to the Skills page
  // automatically. Telegram + Discord have bot-token-direct install
  // paths and don't need this banner.
  const pendingOauth = _wizState.pending_oauth_channel;
  let oauthBanner = '';
  if (pendingOauth === 'slack') {
    oauthBanner = `
      <div style="margin-top:10px;padding:8px 10px;background:rgba(251,191,36,0.10);border:1px solid rgba(251,191,36,0.30);border-radius:6px;font-size:0.78rem;color:var(--text)">
        <b>Slack OAuth pending.</b> Done will take you to the bot's
        Skills page — click <b>Connect Slack</b> there to complete
        the handshake.
      </div>`;
  }

  document.getElementById('wiz-s5-summary').innerHTML = `
    <div><b>${escHtml(botLabel(res.bot_id || _wizState.bot_id))}</b> is live on port <b>${res.port || _wizState.port}</b> (UID ${res.uid || '—'}).</div>
    <div style="color:var(--text2);font-size:0.78rem">Role: ${escHtml(res.role || _wizState.role)} · macOS account: ${escHtml(res.user || _wizState.bot_id)}${res.created_user ? ' (created)' : ' (existing)'}</div>
    <div style="margin-top:8px"><span style="color:var(--text2)">LLM provider:</span> ${llmLabel}</div>
    <div><span style="color:var(--text2)">Messaging:</span> ${channelLabel}</div>
    ${oauthBanner}
  `;
}

// ══════════════════════════════════════════════════════
// Wizard Screen 5 — Verification gauntlet
// Spec: internal/spec-wizard-verification-gauntlet-2026-05-30.md.
// Three-check sequence that replaces the old "All set" Review screen.
// All three checks are automatic. Done button enables once all three
// pass (or operator clicks Skip). The old Check 4 "End-to-end echo"
// was retired 2026-06-01 — pairing is its own beat now.
// ══════════════════════════════════════════════════════

// Three checks — the old "End-to-end echo" (Check 4) was retired
// 2026-06-01 because it promised something the wizard couldn't
// deliver (a UI for entering the OC pairing code). Pairing is now its
// own beat — opened from the Done screen or the Overview tile chip.
// See openPairingWizard() / the "🔗 Pair messaging ID" chip rule.
const _VERIFY_ROW_DEFS = [
  { key: 'ownership',     label: 'File ownership',     manual: false },
  { key: 'agent_dry_run', label: 'Agent can resolve config + credentials', manual: false },
  { key: 'channels',      label: 'Channel handshake',  manual: false },
];

let _verifyState = {
  // Wizard-screen state. Per-tile modal state is held in _verifyModalState
  // so the two can coexist without stepping on each other.
  jobId: null,
  pollTimer: null,
  result: null,
};

let _verifyModalState = {
  botId: null,
  jobId: null,
  pollTimer: null,
  result: null,
};

function _verifyIconFor(status) {
  if (status === 'ok')      return '<span style="color:var(--green)">✓</span>';
  if (status === 'warn')    return '<span style="color:var(--yellow)">⚠</span>';
  if (status === 'fail')    return '<span style="color:var(--red)">✗</span>';
  if (status === 'skip')    return '<span style="color:var(--text3)">↷</span>';
  if (status === 'pending') return '<span style="color:var(--text3)">○</span>';
  return '<span style="color:var(--text3)">⟳</span>';  // running
}

function _verifyRowHtml(row, context, isLast) {
  // context: 'wizard' or 'modal' — controls which DOM ids the fix-ownership button references.
  const status = row.status || 'running';
  const icon = _verifyIconFor(status);
  const labelColor = status === 'fail' ? 'var(--red)' : status === 'warn' ? 'var(--yellow)' : 'var(--text)';
  const def = _VERIFY_ROW_DEFS.find(d => d.key === row.name) || { label: row.name };
  const sep = isLast ? '' : 'border-bottom:1px solid var(--border);';
  const summary = escHtml(row.summary || '');
  let detailHtml = '';
  if (row.detail) {
    detailHtml = `<details style="margin-top:4px;font-size:0.74rem;color:var(--text2)"><summary style="cursor:pointer">Details</summary><pre style="margin:6px 0 0;white-space:pre-wrap;background:var(--bg2);padding:8px;border-radius:4px;font-family:ui-monospace,Menlo,monospace;max-height:220px;overflow:auto">${escHtml(row.detail)}</pre></details>`;
  }
  // Row-specific affordances
  let actionHtml = '';
  if (row.name === 'ownership' && status === 'fail' && row.fix_available) {
    const issuesJson = encodeURIComponent(JSON.stringify(row.issues || []));
    actionHtml = `<button class="btn btn-ghost btn-sm" style="margin-left:8px;font-size:0.74rem" onclick="_verifyFixOwnership('${context}', '${issuesJson}')">Fix ownership</button>`;
  } else if (row.fix_hint) {
    actionHtml = `<div style="margin-top:4px;font-size:0.74rem;color:var(--text2)">${escHtml(row.fix_hint)}</div>`;
  }
  return `<div data-row="${row.name}" style="display:flex;flex-direction:column;gap:2px;padding:8px 0;${sep}">
    <div style="display:flex;gap:8px;align-items:flex-start">
      <div style="width:18px;text-align:center;flex:0 0 auto;line-height:1.2">${icon}</div>
      <div style="flex:1 1 auto">
        <div style="color:${labelColor};font-weight:500">${escHtml(def.label)}</div>
        ${summary ? `<div style="color:var(--text2);font-size:0.78rem">${summary}</div>` : ''}
        ${detailHtml}
      </div>
      ${actionHtml}
    </div>
  </div>`;
}

function _verifyRenderRows(containerId, checks, context) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!checks || checks.length === 0) {
    el.innerHTML = '<div style="color:var(--text3);font-size:0.78rem;padding:6px 0">Starting verification…</div>';
    return;
  }
  // Sort checks to match the canonical order
  const order = _VERIFY_ROW_DEFS.map(d => d.key);
  const sorted = checks.slice().sort((a, b) => order.indexOf(a.name) - order.indexOf(b.name));
  el.innerHTML = sorted.map((r, i) => _verifyRowHtml(r, context, i === sorted.length - 1)).join('');
}

// ── Wizard Screen 5 flow ────────────────────────────────────────────────────

async function _wizVerifyStart() {
  // Reset state
  if (_verifyState.pollTimer)     { clearTimeout(_verifyState.pollTimer); _verifyState.pollTimer = null; }
  _verifyState = { jobId: null, pollTimer: null, result: null };

  document.getElementById('wiz-s5-summary-wrap').style.display = 'none';
  document.getElementById('wiz-s5-done').disabled = true;
  document.getElementById('wiz-s5-retry').style.display = 'none';
  // Reset action rows: show the running row, hide the post-pass fork.
  // Needed when the operator clicks Retry after a fail — without this
  // they'd see both rows simultaneously.
  const runningRow = document.getElementById('wiz-s5-actions-running');
  const doneRow = document.getElementById('wiz-s5-actions-done');
  if (runningRow) runningRow.style.display = 'flex';
  if (doneRow) doneRow.style.display = 'none';
  _verifyRenderRows('wiz-s5-rows', null, 'wizard');

  const r = await api('POST', '/api/wizard/verify/start', {
    bot_id: _wizState.bot_id,
  });
  if (!r || !r.jobId) {
    _verifyRenderRows('wiz-s5-rows', [
      { name: 'agent_dry_run', status: 'fail',
        summary: 'Could not start verify job: ' + (r?.error || 'unknown') },
    ], 'wizard');
    document.getElementById('wiz-s5-retry').style.display = '';
    return;
  }
  _verifyState.jobId = r.jobId;
  _wizVerifyPoll();
}

async function _wizVerifyPoll() {
  if (!_verifyState.jobId) return;
  const r = await api('GET', '/api/wizard/verify/' + encodeURIComponent(_verifyState.jobId));
  if (!r) {
    _verifyState.pollTimer = setTimeout(_wizVerifyPoll, 1500);
    return;
  }
  if (r.result && r.result.checks) {
    _verifyState.result = r.result;
    _verifyRenderRows('wiz-s5-rows', r.result.checks, 'wizard');
  }
  if (r.status === 'complete' || r.status === 'failed') {
    _verifyState.pollTimer = null;
    _wizVerifyOnDone();
    return;
  }
  _verifyState.pollTimer = setTimeout(_wizVerifyPoll, 1500);
}

function _wizVerifyOnDone() {
  // Enable Done if all three checks are ok/skip.
  const result = _verifyState.result;
  if (!result) return;
  const blocking = (result.checks || []).some(c => c.status === 'fail' || c.status === 'warn');
  document.getElementById('wiz-s5-retry').style.display = blocking ? '' : 'none';
  document.getElementById('wiz-s5-done').disabled = blocking;
  if (!blocking) {
    document.getElementById('wiz-s5-summary-wrap').style.display = '';
    _wizSwapToDoneActions();
  }
}

function _wizVerifySkip() {
  // Skip the verify gating entirely. The bot tile will still show an
  // "unverified" chip; the operator can re-run from there.
  document.getElementById('wiz-s5-done').disabled = false;
  document.getElementById('wiz-s5-summary-wrap').style.display = '';
  _wizSwapToDoneActions();
}

// Swap from the "verification running" action row (Skip + Done) to the
// post-verification Pair/View tile fork. Channel-aware: if the operator
// chose Slack we keep the existing OAuth handoff (no pairing wizard
// for Slack yet — it's bot-token-direct only on the backend); if no
// channel was set up at all, render only View tile + Close.
function _wizSwapToDoneActions() {
  const runningRow = document.getElementById('wiz-s5-actions-running');
  const doneRow = document.getElementById('wiz-s5-actions-done');
  if (runningRow) runningRow.style.display = 'none';
  if (doneRow) {
    doneRow.style.display = 'flex';
    doneRow.innerHTML = _wizRenderDoneActions();
  }
}

function _wizRenderDoneActions() {
  const channel = (_wizState && _wizState.applied_channel) || 'none';
  const botId = (_wizState && _wizState.bot_id) || '';
  const botName = botId ? botLabel(botId) : 'this bot';
  const pendingOauth = _wizState && _wizState.pending_oauth_channel;

  // Slack OAuth handoff retains its pre-2026-06-01 behavior — the
  // pairing wizard's paste-the-code flow doesn't apply to Slack
  // (OAuth-only, no per-user DM-pairing) and the OAuth Connect button
  // lives on the bot's Skills page. Mirrors the original _wizScreen5Done.
  if (pendingOauth === 'slack') {
    return `
      <button class="btn btn-ghost" style="margin-right:auto" onclick="closeAddBotModal()">Close</button>
      <button class="btn btn-primary" onclick="closeAddBotModal();_wizGotoSkillsPage('slack')">Finish Slack OAuth →</button>`;
  }

  // No channel was set up — pairing isn't applicable. Just give the
  // operator the close-and-go-to-tile path.
  if (channel === 'none') {
    return `
      <button class="btn btn-ghost" style="margin-right:auto" onclick="closeAddBotModal()">Close</button>
      <button class="btn btn-primary" onclick="_wizScreen5DoneViewTile()">View bot tile →</button>`;
  }

  // Hybrid Done — operator picks pair-now or view-the-tile. Both paths
  // converge on Overview (the pairing modal's Done button reloads
  // status and the tile chip clears once pairing succeeds).
  const chCfg = _pairCfgFor(channel);
  const chLabel = chCfg ? chCfg.label : channel;
  const tgUser = (_wizState && _wizState.telegram_bot_username) || null;
  const pairBtnLabel = (channel === 'telegram' && tgUser)
    ? `Pair with @${escHtml(tgUser)} on Telegram →`
    : `Pair with ${escHtml(botName)} on ${escHtml(chLabel)} →`;
  return `
    <button class="btn btn-ghost" style="margin-right:auto" onclick="_wizScreen5DoneViewTile()">View bot tile</button>
    <button class="btn btn-primary" onclick="_wizScreen5DonePair()">${pairBtnLabel}</button>`;
}

// Hand off to the standalone pairing wizard for the just-installed bot.
// Closes the install wizard first so the pairing modal isn't stacked
// on top of it — the operator's mental model is "install is done; now
// pairing." Pairing modal's Done button lands them on Overview where
// the tile-chip pattern reinforces the surface they'll use next time.
function _wizScreen5DonePair() {
  const botId = _wizState && _wizState.bot_id;
  const channel = _wizState && _wizState.applied_channel;
  if (!botId) { closeAddBotModal(); return; }
  closeAddBotModal();
  // Brief delay so the close animation completes before the new modal
  // opens — avoids a visible flash of two overlapping overlays.
  setTimeout(() => openPairingWizard(botId, channel), 60);
}

// Close the install wizard and navigate to Overview. The new bot's
// tile is already in /api/status; the chip lights up the moment the
// modal closes (Overview re-renders on loadStatus). Operator can come
// back to pair later from the chip.
function _wizScreen5DoneViewTile() {
  closeAddBotModal();
  const navEl = document.querySelector('.nav-item[data-page="overview"]');
  if (navEl && typeof nav === 'function') nav(navEl);
}

// Shared by wizard + modal. context = 'wizard' | 'modal'.
async function _verifyFixOwnership(context, issuesJson) {
  let issues;
  try { issues = JSON.parse(decodeURIComponent(issuesJson)); } catch (e) { issues = []; }
  if (!issues.length) { toast('No issues to fix', 'err'); return; }
  const botId = context === 'wizard' ? _wizState.bot_id : _verifyModalState.botId;
  if (!botId) return;
  toast('Repairing ' + issues.length + ' path(s)…', 'info');
  let okCount = 0, failCount = 0;
  for (const issue of issues) {
    const r = await api('POST', `/api/wizard/verify/${encodeURIComponent(botId)}/fix-ownership`,
                        { path: issue.path });
    if (r && r.ok) okCount++; else failCount++;
  }
  if (failCount > 0) {
    toast(`${okCount} fixed, ${failCount} failed — see admin log`, 'err');
  } else {
    toast(`Fixed ${okCount} path(s) — re-running verify`, 'ok');
  }
  // Re-run the gauntlet to confirm the fix
  if (context === 'wizard') _wizVerifyStart();
  else _verifyModalStart();
}

// _verifyArmE2E / _verifyPollE2E removed 2026-06-01 with Check 4.
// Pairing now happens in the standalone pairing wizard modal.
// See openPairingWizard() below.

// ── Per-tile Verify modal ──────────────────────────────────────────────────

function _verifyModalOpen(botId) {
  _verifyModalState = { botId, jobId: null, pollTimer: null, result: null };
  document.getElementById('verify-modal-bot').textContent = botLabel(botId);
  document.getElementById('verify-modal').classList.add('open');
  _verifyModalStart();
}

function _verifyModalClose() {
  if (_verifyModalState.pollTimer) { clearTimeout(_verifyModalState.pollTimer); }
  _verifyModalState = { botId: null, jobId: null, pollTimer: null, result: null };
  document.getElementById('verify-modal').classList.remove('open');
}

async function _verifyModalStart() {
  if (_verifyModalState.pollTimer) { clearTimeout(_verifyModalState.pollTimer); }
  _verifyModalState.jobId = null;
  _verifyModalState.result = null;
  document.getElementById('verify-modal-retry').style.display = 'none';
  _verifyRenderRows('verify-modal-rows', null, 'modal');

  const r = await api('POST', '/api/wizard/verify/start', {
    bot_id: _verifyModalState.botId,
  });
  if (!r || !r.jobId) {
    _verifyRenderRows('verify-modal-rows', [
      { name: 'agent_dry_run', status: 'fail',
        summary: 'Could not start verify job: ' + (r?.error || 'unknown') },
    ], 'modal');
    document.getElementById('verify-modal-retry').style.display = '';
    return;
  }
  _verifyModalState.jobId = r.jobId;
  _verifyModalPoll();
}

async function _verifyModalPoll() {
  if (!_verifyModalState.jobId) return;
  const r = await api('GET', '/api/wizard/verify/' + encodeURIComponent(_verifyModalState.jobId));
  if (!r) {
    _verifyModalState.pollTimer = setTimeout(_verifyModalPoll, 1500);
    return;
  }
  if (r.result && r.result.checks) {
    _verifyModalState.result = r.result;
    _verifyRenderRows('verify-modal-rows', r.result.checks, 'modal');
  }
  if (r.status === 'complete' || r.status === 'failed') {
    _verifyModalState.pollTimer = null;
    document.getElementById('verify-modal-retry').style.display = '';
    return;
  }
  _verifyModalState.pollTimer = setTimeout(_verifyModalPoll, 1500);
}

// ════════════════════════════════════════════════════════════════════════
// Page: Settings (Modules + Pod Config)
//
// Two Settings subtabs as one contiguous block:
//
//   Modules subtab — renderModules + saveSetting + toggleModule +
//                    toggleDetector.
//
//   Pod Config subtab — populateConfig + renderPodAdminsList + the
//     full save flow family (saveAlerts, savePrimary, saveAppTesting,
//     saveBackupSettings, saveHealSettings, savePodIdentity,
//     saveSecuritySettings, saveClassifiers), renderModelsTable, and
//     the pod-admin add/remove flow.
//
// Plus tierResolutionJumpToAiOptimization — the "→ Edit on AI
// Optimization" deep-link helper for the Tier Resolution table.
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - api(), toast(), escHtml(), botLabel() — core/
//   - aiSwitchBot — pages/ai-optimization.js (jump target)
//   - nav() / window.nav — core/router.js
//   - loadStatus() (Overview) — refreshed after any setting save
//   - The substrate-audit chip helpers (_renderSubstrateAuditRows,
//     _substrateAuditPill, _runSubstrateAuditNow) now live in
//     pages/skills.js (Phase 3r) — called by Plugins page via global
//     lookup, no longer part of Settings.
//
// Out of scope (separate clusters):
//   loadModelCatalog + openModelCatalogModal + submitModelCatalog +
//   removeModelFromCatalog (~line 18548) — Settings → Models catalog;
//   future cleanup phase.
// ════════════════════════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════
// Modules
// ══════════════════════════════════════════════════════
function renderModules() {
  const el = document.getElementById('modules-grid');
  const d = _modulesData;

  const SETTING_LABELS = {
    checkIntervalMin:      'Check gateway health every (minutes)',
    failuresBeforeAlert:   'Alert after (N) consecutive failures',
    slowThresholdMs:       'Consider slow if response > (ms)',
    restartCooldownMin:    'Wait between restarts (minutes)',
    retentionDays:         'Keep metrics for (days)',
    sampleSize:            'Conversations to sample per audit',
    accuracyFloor:         'Minimum acceptable accuracy (%)',
    minSessionsForTheme:   'Minimum sessions before suggesting application',
  };

  function settingsBlock(key, keys) {
    const cfg = d[key] || {};
    if (!keys.length) return '';
    return `<div class="module-settings" style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border)">
      ${keys.map(k => `<div class="module-setting">
        <label>${SETTING_LABELS[k] || k}</label>
        <input type="text" value="${cfg[k] ?? ''}" onchange="tuneModule('${key}','${k}',this.value)" style="width:80px">
      </div>`).join('')}
    </div>`;
  }

  function featureCard(f) {
    const cfg = d[f.key] || {};
    const enabled = cfg.enabled !== false;
    const noteHtml = f.note ? `<div class="module-cost" style="margin-top:3px">Note: ${f.note}</div>` : '';
    const bcHtml = `<div class="module-feature-bc">
      ${f.benefit ? `<span class="module-benefit">Benefit: ${f.benefit}</span>` : ''}
      ${f.cost    ? `<span class="module-cost">Cost: ${f.cost}</span>` : ''}
      ${noteHtml}
    </div>`;
    const toggleHtml = !f.noToggle ? `<div class="module-feature-toggle">
      <label class="toggle">
        <input type="checkbox" ${enabled ? 'checked' : ''} onchange="toggleModule('${f.key}',this.checked)">
        <span class="toggle-slider"></span>
      </label>
    </div>` : '';
    return `<div class="module-feature-card${f.disabled ? ' module-disabled' : ''}">
      <div class="module-feature-top">
        <div class="module-feature-info">
          <div class="module-feature-name">${f.name}</div>
          <div class="module-feature-desc">${f.desc}</div>
          ${bcHtml}
        </div>
        ${toggleHtml}
      </div>
      ${settingsBlock(f.key, f.settings || [])}
    </div>`;
  }

  // ── Core Loop (master switch) ───────────────────────────────────────────
  const rsiEnabled = (d.rsi || {}).enabled !== false;
  const coreLoopHtml = `<div class="module-loop-card${rsiEnabled ? '' : ' module-disabled'}">
    <div class="module-loop-header">
      <div style="flex:1;min-width:0">
        <div class="module-loop-title">RSI Feedback Loop</div>
        <div class="module-loop-pipeline">
          Observer <span class="sep">→</span> Metrics <span class="sep">→</span> Analysis <span class="sep">→</span> Apply <span class="sep">→</span> Outcomes
        </div>
        <div class="module-feature-bc" style="margin-top:8px">
          <span class="module-benefit">Benefit: Bots keep improving — Evolve detects cost, security, and reliability issues and proposes fixes you approve</span>
          <span class="module-cost">Cost: AI calls for weekly analysis + per-proposal verification</span>
        </div>
      </div>
      <div class="module-feature-toggle">
        <label class="toggle">
          <input type="checkbox" ${rsiEnabled ? 'checked' : ''} onchange="toggleModule('rsi',this.checked)">
          <span class="toggle-slider"></span>
        </label>
      </div>
    </div>
    <div class="module-loop-footer">
      ${rsiEnabled
        ? `<button class="module-loop-link" onclick="document.querySelector('[data-page=self-improvement]').click()">View Proposals →</button>`
        : `<div class="module-cost">Off: bots keep running and dashboards stay populated, but Evolve stops generating recommendations and applying improvements — no model tokens spent on the analyze/apply/outcome loop. Other costed work (app audits, the app scanner, observation extraction) is not part of this loop and keeps spending; manage it from its own controls.</div>`
      }
    </div>
  </div>`;

  // ── Safety (pod-wide) ───────────────────────────────────────────────────
  // The `cost` module is a safety gate, not a feature: it controls
  // spend_alert.py, which includes the L1 spend-breaker auto-trip. Disabling
  // it removes automatic spend protection, so it gets a dedicated card with an
  // explicit off-state warning rather than living in Optional Features.
  // (The cost_watchdog antipattern daemon is $0 and runs independently of this
  // toggle — the off copy says so to keep the mental model honest.)
  const costEnabled = (d.cost || {}).enabled !== false;
  const safetyHtml = `<div class="module-section-title">Safety</div>
    <hr class="module-section-divider">
    <div class="module-feature-list">
      <div class="module-feature-card${costEnabled ? '' : ' module-warn-card'}">
        <div class="module-feature-top">
          <div class="module-feature-info">
            <div class="module-feature-name">Spend Protection</div>
            <div class="module-feature-desc">Real-time spend monitoring plus the automatic L1 spend breaker. When a bot crosses its daily cap the breaker trips and pauses its background activity (heartbeat + autonomous work) while keeping user chat alive.</div>
            <div class="module-feature-bc">
              <span class="module-benefit">Benefit: A runaway loop can't burn your budget unbounded — spend is capped automatically</span>
              <span class="module-cost">Cost: No AI cost — reads usage logs</span>
              ${costEnabled
                ? ''
                : `<span class="module-warn">Off: real-time spend alerts and the automatic spend breaker are both disabled — a runaway bot can spend without limit until you intervene manually. The zero-cost cost-antipattern watchdog keeps running and is unaffected by this switch.</span>`}
            </div>
          </div>
          <div class="module-feature-toggle">
            <label class="toggle">
              <input type="checkbox" ${costEnabled ? 'checked' : ''} onchange="toggleModule('cost',this.checked)">
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>
      </div>
    </div>`;

  // ── Optional Features (pod-wide) ────────────────────────────────────────
  const OPTIONAL = [
    {
      key: 'expansion',
      name: 'Expansion',
      desc: "Monthly analysis that finds things your bot does regularly that don't have formal application manifests yet. Suggests new applications to track.",
      benefit: 'Discovers undocumented applications automatically',
      cost: '~5 AI calls per month (Haiku)',
      settings: ['minSessionsForTheme'],
    },
    {
      key: 'continuity_engine',
      name: 'Continuity Engine',
      desc: 'When the bot commits to acting later ("I\'ll get back to you in 20 minutes"), it calls the `defer` plugin tool and a 2-minute runner fires the follow-up at the scheduled time. Default-on for every bot; opt out per-bot via the ⏱ pill on the bot tile in the Overview (use for read-only bots, security bots, or anything that shouldn\'t make time-deferred promises).',
      benefit: 'Bot delivers on time-deferred commitments without re-prompting',
      cost: 'No background AI cost — runner only spends model tokens when a defer fires',
      settings: [],
      noToggle: true,
    },
    {
      key: 'healing',
      name: 'Healing',
      desc: "Monitors each bot's gateway health every few minutes. Automatically restarts a gateway if it becomes unresponsive.",
      benefit: 'Self-healing infrastructure, fewer manual restarts',
      cost: 'No AI cost — process monitoring only',
      settings: ['checkIntervalMin', 'failuresBeforeAlert', 'slowThresholdMs', 'restartCooldownMin'],
    },
  ];

  const optionalHtml = `<div class="module-section-title">Optional Features</div>
    <hr class="module-section-divider">
    <div class="module-feature-list">${OPTIONAL.map(featureCard).join('')}</div>`;

  // ── Group 3: Advanced (collapsed) ──────────────────────────────────────
  const ADVANCED = [
    {
      key: 'slack_signals',
      name: 'Slack Quality Signals',
      desc: "Monitors how your team reacts to Team_bot_a's Slack messages (👍/👎/etc.) and uses that as a quality signal for the RSI loop.",
      benefit: "Uses real team feedback to improve Team_bot_a's Slack performance",
      cost: 'No AI cost — reads Slack reaction data',
      note: 'Only relevant for bots with Slack enabled (Team_bot_a). No effect on Admin_bot/Security_bot.',
      settings: [],
    },
    {
      key: 'community_intel',
      name: 'Community Intelligence',
      desc: 'Aggregates quality signals across community deployments.',
      benefit: '',
      cost: '',
      note: 'Not yet available for this deployment.',
      settings: [],
      disabled: true,
      noToggle: true,
    },
  ];

  const advancedHtml = `<details class="module-advanced-section">
    <summary class="module-advanced-summary"><span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span> Advanced</summary>
    <div class="module-advanced-body">
      <div class="module-feature-list">${ADVANCED.map(featureCard).join('')}</div>
    </div>
  </details>`;

  el.innerHTML = coreLoopHtml + safetyHtml + optionalHtml + advancedHtml;
}

async function toggleModule(name, enabled) {
  const r = await api('POST', `/api/modules/${name}/${enabled?'enable':'disable'}`);
  toast(r.ok ? `✓ ${name} ${enabled?'enabled':'disabled'}` : '✗ Failed', r.ok?'ok':'err');
  if (r.ok) { await loadModules(); }
}
async function tuneModule(name, key, value) {
  const parsed = isNaN(value) ? value === 'true' ? true : value === 'false' ? false : value : Number(value);
  await api('POST', `/api/modules/${name}/tune`, { key, value: parsed });
  toast(`✓ ${name}.${key} = ${parsed}`, 'ok');
}
async function toggleDetector(name, enabled) {
  const r = await api('POST', `/api/modules/detector/${name}/${enabled?'enable':'disable'}`);
  toast(r.ok ? `✓ detector ${name} ${enabled?'enabled':'disabled'}` : '✗ Failed', r.ok?'ok':'err');
}

// ══════════════════════════════════════════════════════
// Config
// ══════════════════════════════════════════════════════
function populateConfig() {
  const a = _networkData.alerts || {};
  document.getElementById('cfg-channel').value = a.channel || '';
  document.getElementById('cfg-chatid').value = a.chatId || '';
  // Independent security-alert channel status comes from a dedicated
  // endpoint (keystore presence, never the token) — fire-and-forget so the
  // synchronous network-data populate isn't blocked on it.
  refreshSecurityAlertStatus();
  // Primary Bot dropdown was retired 2026-05-20 — see the explanatory
  // comment near the (now removed) card markup in this file. Every
  // Evolve install ships with a dedicated 'evo' primary bot; the
  // operator-facing chooser added confusion without a real decision
  // to make. ``network.primary`` is still in the JSON; only this UI
  // surface is gone.
  const at = _networkData.app_testing || {};
  document.getElementById('cfg-app-testing-cadence').value = at.default_cadence || 'light';
  document.getElementById('cfg-app-testing-scheduler').checked = !!at.scheduler_enabled;
  document.getElementById('cfg-app-testing-max-runs').value =
    (at.max_runs_per_tick != null ? at.max_runs_per_tick : 10);
  const bkAcct = document.getElementById('cfg-backup-default-account');
  if (bkAcct) bkAcct.value = _networkData.defaultBackupAccount || '';
  const bkKey = document.getElementById('cfg-backup-ssh-key');
  if (bkKey) bkKey.value = _networkData.backupSshKey || '';

  // Heal pod-wide defaults — leave inputs blank when the key is absent so
  // the placeholder shows the documented default (3, 24, 3000, 10, 5).
  const heal = _networkData.heal || {};
  const _setNumIfPresent = (id, val) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = (val === undefined || val === null) ? '' : val;
  };
  _setNumIfPresent('cfg-heal-failures-before', heal.failuresBeforeProposal);
  _setNumIfPresent('cfg-heal-window-hours', heal.windowHours);
  _setNumIfPresent('cfg-heal-slow-ms', heal.slowThresholdMs);
  _setNumIfPresent('cfg-heal-cooldown-min', heal.restartCooldownMin);
  _setNumIfPresent('cfg-heal-check-timeout-sec', heal.checkTimeoutSec);

  // Classifiers — defaults are tier3 / keyword / tier0; only override the
  // pre-selected option when network.json actually sets a different value.
  const cls = _networkData.classifiers || {};
  const _setSelectIfPresent = (id, val) => {
    const el = document.getElementById(id);
    if (!el || !val) return;
    el.value = val;
  };
  _setSelectIfPresent('cfg-cls-tier-tier', cls.tier && cls.tier.tier);
  _setSelectIfPresent('cfg-cls-tier-fallback', cls.tier && cls.tier.fallback);
  _setSelectIfPresent('cfg-cls-judge-tier', cls.judge && cls.judge.tier);

  // Security — mode + requireForge + autoRejectRisk checkbox array.
  // Defaults match review.py: mode=primary, requireForge=false,
  // autoRejectRisk=["high","critical"] when the key is absent.
  const sec = _networkData.security || {};
  _setSelectIfPresent('cfg-sec-mode', sec.mode || 'primary');
  const reqForge = document.getElementById('cfg-sec-require-forge');
  if (reqForge) reqForge.checked = !!sec.requireForge;
  const risks = Array.isArray(sec.autoRejectRisk) ? sec.autoRejectRisk : ['high', 'critical'];
  ['low', 'medium', 'high', 'critical'].forEach(level => {
    const el = document.getElementById('cfg-sec-risk-' + level);
    if (el) el.checked = risks.includes(level);
  });

  // Timezone — input is the optional override. Empty input means
  // "use detected." The subtitle shows what /etc/localtime resolved
  // to so the operator knows what they'd inherit if they cleared.
  const tzEl = document.getElementById('cfg-timezone');
  if (tzEl) tzEl.value = _networkData.timezone || '';
  const tzDet = document.getElementById('cfg-timezone-detected-val');
  if (tzDet) {
    tzDet.textContent = _networkData.timezone_detected
      || '(could not read /etc/localtime)';
  }

  // Pod Identity — passphrases + ssh_target. Passphrases are stored
  // plaintext in network.json (shared secrets, same threat model as
  // any other plaintext config), so showing them isn't extra exposure.
  const pod = _networkData.pod || {};
  const podAdmin = document.getElementById('cfg-pod-admin-pp');
  if (podAdmin) podAdmin.value = pod.admin_passphrase || '';
  const podPrim = document.getElementById('cfg-pod-primary-pp');
  if (podPrim) podPrim.value = pod.primary_passphrase || '';
  const podSsh = document.getElementById('cfg-pod-ssh-target');
  if (podSsh) podSsh.value = pod.ssh_target || '';

  // Pod Admins — render the (channel, external_id) list with delete
  // buttons. resolved_names is read-only cache info.
  renderPodAdminsList();
}

function renderPodAdminsList() {
  const el = document.getElementById('cfg-pod-admins-body');
  if (!el) return;
  const pod = _networkData?.pod || {};
  const admins = pod.admins || {};
  // Tolerate the legacy bare-string shape (M1-B2) — a hand-edited pod
  // block would otherwise render as "no admins configured".
  const externalIds = admins.external_ids || {};
  const resolved = admins.resolved_names || {};
  const rows = [];
  for (const [channel, raw] of Object.entries(externalIds)) {
    const ids = (Array.isArray(raw) ? raw : [raw]).filter(x => x || x === 0);
    for (const id of ids) {
      const cached = resolved[`${channel}:${id}`];
      const name = cached?.name || cached?.username || '';
      rows.push({ channel, id, name });
    }
  }
  if (!rows.length) {
    el.innerHTML = '<div class="subtle" style="font-size:0.82rem;padding:4px 0">No channel-level admins configured. Anyone who claims via passphrase still becomes admin.</div>';
    return;
  }
  el.innerHTML = `
    <table class="resp-table" style="width:100%;font-size:0.82rem">
      <thead><tr>
        <th style="text-align:left;padding:4px 8px">Channel</th>
        <th style="text-align:left;padding:4px 8px">External ID</th>
        <th style="text-align:left;padding:4px 8px">Resolved Name</th>
        <th style="text-align:right;padding:4px 8px;width:60px"></th>
      </tr></thead>
      <tbody>
        ${rows.map(r => `<tr>
          <td data-label="Channel" style="padding:4px 8px"><code>${escHtml(r.channel)}</code></td>
          <td data-label="External ID" style="padding:4px 8px"><code>${escHtml(r.id)}</code></td>
          <td data-label="Resolved" style="padding:4px 8px;color:var(--text2)">${escHtml(r.name) || '<span style="color:var(--text3)">unresolved</span>'}</td>
          <td data-label="" style="padding:4px 8px;text-align:right"><button class="btn btn-ghost btn-sm" onclick="removePodAdmin('${escHtml(r.channel)}','${escHtml(r.id)}')" title="Remove">✕</button></td>
        </tr>`).join('')}
      </tbody>
    </table>
  `;
}

async function saveAlerts() {
  const r = await api('POST', '/api/config', { action: 'alerts', alerts: {
    channel: document.getElementById('cfg-channel').value,
    chatId: document.getElementById('cfg-chatid').value,
  }});
  toast(r.ok ? '✓ Alerts saved' : '✗ Failed', r.ok?'ok':'err');
}

// ── Independent security alert channel (opt-in) ──────────────────────────
// Replaces the wizard's old forced "second Telegram bot" step. The token is
// write-only from the UI's side — the status fetch reports presence + chat
// id only, never the token itself (see routes_alerts security-alert routes).
function _renderSecurityAlertStatus(s) {
  const el = document.getElementById('cfg-secalert-status');
  if (!el) return;
  if (s && s.securityAlertConfigured) {
    el.innerHTML = `Dedicated channel configured — token <code>•••• configured</code>`
      + (s.chatId ? `, chat ID <code>${escHtml(s.chatId)}</code>.` : '.')
      + ` Security alerts use this bot instead of the main one.`;
  } else {
    el.textContent = 'Not configured — security alerts use the main Evolve alert channel.';
  }
}
async function refreshSecurityAlertStatus() {
  try {
    const s = await api('GET', '/api/config/security-alert');
    _renderSecurityAlertStatus(s);
    // Show the saved chat ID so re-saving doesn't require re-typing it; the
    // token field stays blank (write-only) by design.
    const chatEl = document.getElementById('cfg-secalert-chatid');
    if (chatEl && s && s.chatId && !chatEl.value) chatEl.value = s.chatId;
  } catch (_e) { /* leave the placeholder */ }
}
async function saveSecurityAlert() {
  const token = (document.getElementById('cfg-secalert-token')?.value || '').trim();
  const chatId = (document.getElementById('cfg-secalert-chatid')?.value || '').trim();
  if (!token || !chatId) {
    toast('✗ Bot token and chat ID are both required', 'err');
    return;
  }
  const r = await api('POST', '/api/config/security-alert', { op: 'set', token, chatId });
  if (r.ok) {
    toast('✓ Security alert channel saved', 'ok');
    document.getElementById('cfg-secalert-token').value = '';  // never retain the secret in the DOM
    _renderSecurityAlertStatus(r);
  } else {
    toast('✗ ' + ((r && r.error) || 'Failed to save security channel'), 'err');
  }
}
async function testSecurityAlert() {
  // Test the typed token if present, else the stored one (server falls back).
  const token = (document.getElementById('cfg-secalert-token')?.value || '').trim();
  const r = await api('POST', '/api/config/security-alert', { op: 'test', token });
  if (r && r.ok) {
    toast(`✓ Connected to @${r.username || 'bot'}`, 'ok');
  } else {
    toast('✗ ' + ((r && r.error) || 'Telegram test failed'), 'err');
  }
}
async function clearSecurityAlert() {
  if (!await confirmModal({ body: 'Disable the independent security alert channel? Security alerts will revert to the main Evolve bot.', danger: true })) return;
  const r = await api('POST', '/api/config/security-alert', { op: 'clear' });
  if (r.ok) {
    toast('✓ Security alert channel cleared', 'ok');
    document.getElementById('cfg-secalert-token').value = '';
    document.getElementById('cfg-secalert-chatid').value = '';
    _renderSecurityAlertStatus(r);
  } else {
    toast('✗ ' + ((r && r.error) || 'Failed to clear security channel'), 'err');
  }
}
// Export the inline-onclick handlers (index.html Settings → Pod Config card).
window.saveSecurityAlert = saveSecurityAlert;
window.testSecurityAlert = testSecurityAlert;
window.clearSecurityAlert = clearSecurityAlert;

// savePrimary() retired 2026-05-20 along with its UI card. The
// POST /api/config?action=primary route is still wired so the CLI
// (also being deprecated) keeps working, but the chat surface is
// gone. A no-op stub remains in case any third-party automation
// or stale browser tab still calls it — it surfaces a console
// warning so the cause is visible.
async function savePrimary() {
  console.warn(
    'savePrimary() is retired — primary bot is fixed at install time. ' +
    'See the wizard for new pods, or `evolve-admin config set-primary` ' +
    '(also deprecated) for legacy CLI use.',
  );
}

async function saveAppTesting() {
  const maxRunsRaw = document.getElementById('cfg-app-testing-max-runs').value;
  const maxRuns = maxRunsRaw === '' ? 10 : parseInt(maxRunsRaw, 10);
  const r = await api('POST', '/api/config', { action: 'appTesting', appTesting: {
    default_cadence: document.getElementById('cfg-app-testing-cadence').value,
    scheduler_enabled: document.getElementById('cfg-app-testing-scheduler').checked,
    max_runs_per_tick: Number.isFinite(maxRuns) && maxRuns >= 0 ? maxRuns : 10,
  }});
  toast(r.ok ? '✓ App testing saved' : '✗ Failed', r.ok?'ok':'err');
}

// Save the Backup card's pod-wide defaults (Maintenance → Backup behaviour).
// Empty strings are sent as null so the operator can clear a setting and
// fall back to per-bot keys / generic URL placeholders.
async function saveBackupSettings() {
  const acct = document.getElementById('cfg-backup-default-account').value.trim();
  const keyPath = document.getElementById('cfg-backup-ssh-key').value.trim();
  const r = await api('POST', '/api/config', {
    action: 'backup',
    backup: {
      defaultBackupAccount: acct || null,
      backupSshKey: keyPath || null,
    },
  });
  if (r.ok) {
    toast('✓ Backup settings saved', 'ok');
    // Refresh the cached network data so the Backup panel picks up the
    // new shared-key / default-account values without a hard reload.
    _networkData = await api('GET', '/api/network');
    populateConfig();
  } else {
    toast('✗ Failed to save backup settings', 'err');
  }
}

// Save the Self-Healing card. Empty inputs are sent as null so the
// heal daemon falls back to its hardcoded defaults — clearer than
// freezing whatever placeholder value the form happened to show.
async function saveHealSettings() {
  const _intOrNull = (id) => {
    const raw = (document.getElementById(id)?.value || '').trim();
    if (raw === '') return null;
    const n = parseInt(raw, 10);
    return Number.isFinite(n) && n >= 0 ? n : null;
  };
  const r = await api('POST', '/api/config', {
    action: 'heal',
    heal: {
      failuresBeforeProposal: _intOrNull('cfg-heal-failures-before'),
      windowHours: _intOrNull('cfg-heal-window-hours'),
      slowThresholdMs: _intOrNull('cfg-heal-slow-ms'),
      restartCooldownMin: _intOrNull('cfg-heal-cooldown-min'),
      checkTimeoutSec: _intOrNull('cfg-heal-check-timeout-sec'),
    },
  });
  if (r.ok) {
    toast('✓ Self-healing saved', 'ok');
    _networkData = await api('GET', '/api/network');
    populateConfig();
  } else {
    toast('✗ Failed to save self-healing', 'err');
  }
}

// Save the Pod Identity card. Passphrases + ssh_target. Empty values
// clear the field (so config.py's _POD_FIELD_DEFAULTS kicks in for
// passphrases — "charles" / "darwin" — and the SSH target falls back
// to "" meaning "local").
async function savePodIdentity() {
  const adminPp = (document.getElementById('cfg-pod-admin-pp')?.value || '').trim();
  const primaryPp = (document.getElementById('cfg-pod-primary-pp')?.value || '').trim();
  const sshTarget = (document.getElementById('cfg-pod-ssh-target')?.value || '').trim();
  const r = await api('POST', '/api/config', {
    action: 'pod',
    pod: {
      admin_passphrase: adminPp || null,
      primary_passphrase: primaryPp || null,
      ssh_target: sshTarget || null,
    },
  });
  if (r.ok) {
    toast('✓ Pod identity saved', 'ok');
    _networkData = await api('GET', '/api/network');
    populateConfig();
  } else {
    toast('✗ Failed to save pod identity', 'err');
  }
}

// Save the Pod Timezone card. Empty input clears the override, then
// resolve_pod_timezone() auto-detects from /etc/localtime (falling back
// to America/Los_Angeles only when both fail). The server validates the
// IANA name with zoneinfo, so a typo surfaces in the toast.
async function saveTimezone() {
  const tz = (document.getElementById('cfg-timezone')?.value || '').trim();
  const r = await api('POST', '/api/config', {
    action: 'timezone',
    timezone: tz || null,
  });
  if (r.ok) {
    toast('✓ Timezone saved', 'ok');
    _networkData = await api('GET', '/api/network');
    populateConfig();
  } else {
    toast('✗ ' + ((r && r.error) || 'Failed to save timezone'), 'err');
  }
}

// Add a (channel, external_id) pair to pod.admins.external_ids[channel].
async function addPodAdmin() {
  const channel = (document.getElementById('cfg-pod-admin-channel')?.value || '').trim();
  const extId = (document.getElementById('cfg-pod-admin-extid')?.value || '').trim();
  if (!channel || !extId) {
    toast('✗ Channel and external ID are required', 'err');
    return;
  }
  const r = await api('POST', '/api/config', {
    action: 'podAdmins',
    podAdmins: { op: 'add', channel, external_id: extId },
  });
  if (r.ok) {
    toast(`✓ Added ${channel}:${extId} as admin`, 'ok');
    document.getElementById('cfg-pod-admin-extid').value = '';
    _networkData = await api('GET', '/api/network');
    renderPodAdminsList();
  } else {
    toast('✗ ' + ((r && r.error) || 'Failed to add admin'), 'err');
  }
}

async function removePodAdmin(channel, extId) {
  if (!await confirmModal({ body: `Remove ${channel}:${extId} from pod admins?`, danger: true })) return;
  const r = await api('POST', '/api/config', {
    action: 'podAdmins',
    podAdmins: { op: 'remove', channel, external_id: extId },
  });
  if (r.ok) {
    toast(`✓ Removed ${channel}:${extId}`, 'ok');
    _networkData = await api('GET', '/api/network');
    renderPodAdminsList();
  } else {
    toast('✗ ' + ((r && r.error) || 'Failed to remove admin'), 'err');
  }
}

// Save the Security card. mode + requireForge + the auto-reject risk
// list (assembled from 4 checkboxes). Backend allowlists and merges
// into existing security dict so botId / rulesFile / future fields
// survive when not touched by this UI.
async function saveSecuritySettings() {
  const risks = ['low', 'medium', 'high', 'critical'].filter(
    level => document.getElementById('cfg-sec-risk-' + level)?.checked,
  );
  const r = await api('POST', '/api/config', {
    action: 'security',
    security: {
      mode: document.getElementById('cfg-sec-mode')?.value || 'primary',
      requireForge: !!document.getElementById('cfg-sec-require-forge')?.checked,
      autoRejectRisk: risks,
    },
  });
  if (r.ok) {
    toast('✓ Security saved', 'ok');
    _networkData = await api('GET', '/api/network');
    populateConfig();
  } else {
    toast('✗ Failed to save security settings', 'err');
  }
}

// Save the Classifiers card. Three nested keys; backend sets them on the
// classifiers.{tier|judge} sub-dicts so existing per-classifier overrides
// (e.g. a future judge.confidence_threshold) survive the save.
async function saveClassifiers() {
  const r = await api('POST', '/api/config', {
    action: 'classifiers',
    classifiers: {
      tier: {
        tier: document.getElementById('cfg-cls-tier-tier').value,
        fallback: document.getElementById('cfg-cls-tier-fallback').value,
      },
      judge: {
        tier: document.getElementById('cfg-cls-judge-tier').value,
      },
    },
  });
  if (r.ok) {
    toast('✓ Classifiers saved', 'ok');
    _networkData = await api('GET', '/api/network');
    populateConfig();
  } else {
    toast('✗ Failed to save classifiers', 'err');
  }
}

async function renderModelsTable() {
  const el = document.getElementById('models-table');
  if (!el) return;
  let res;
  try {
    res = await api('GET', '/api/models/tier-resolution');
  } catch (e) {
    el.innerHTML = `<div class="empty">Failed to load: ${escHtml(String(e))}</div>`;
    return;
  }
  if (res && res.error) { el.innerHTML = `<div class="empty">${escHtml(res.error)}</div>`; return; }
  const tiers = (res && res.tiers) || {};
  const primaryBot = res && res.primary_bot;
  const order = ['tier0', 'tier1', 'tier2', 'tier3'];
  // Resolution chain after the 2026-05-25 simplification: primary bot's
  // tier_assignments → DEFAULT_TIERS. The pod-wide models.tiers override
  // layer was retired — admin layer mirrors evo by design, one config
  // layer instead of two.
  const rows = order.filter(t => tiers[t]).map(tid => {
    const tc = tiers[tid];
    let sourceLabel, sourceCls, sourceTitle;
    if (tc.source && tc.source.indexOf('primary bot tier_assignments') === 0) {
      sourceLabel = `${primaryBot || 'primary'} override`;
      sourceCls = 'ok';
      sourceTitle = `From the primary bot's tier_assignments — set on the AI Optimization page for ${primaryBot}.`;
    } else {
      sourceLabel = 'default';
      sourceCls = 'member';
      sourceTitle = 'Hardcoded fallback (DEFAULT_TIERS in analyzer/models.py). Set values on AI Optimization for the primary bot to override.';
    }
    return `<tr>
      <td>${badge(tid,'member')}</td>
      <td>${escHtml(tc.name || '—')}</td>
      <td style="font-family:monospace;font-size:0.78rem;color:var(--teal)">${escHtml(tc.primary || '—')}</td>
      <td style="font-size:0.75rem;color:var(--text2)">${escHtml((tc.fallbacks || []).join(', ') || '—')}</td>
      <td>${badge(tc.costClass || '?', tc.costClass === 'high' ? 'crit' : tc.costClass === 'medium' ? 'warn' : 'ok')}</td>
      <td title="${escHtml(sourceTitle)}">${badge(sourceLabel, sourceCls)}</td>
    </tr>`;
  }).join('');
  // Footer explains the mirror relationship explicitly (Evolve admin tiers
  // are not configured here — they read from <primary>'s evolve-tiers.json,
  // which AI Optimization writes) and offers a one-click deep-link to the
  // place where edits actually land. Without this, operators staring at
  // this table reasonably expect that changing anything here would do
  // something — but the table is read-only by design.
  let footer;
  if (primaryBot) {
    const label = escHtml(botLabel(primaryBot));
    const bid = escHtml(primaryBot).replace(/'/g, "\\'");
    footer = `The Evolve model tiers mirror <strong>${label}</strong>'s tier definitions — engine background calls (analyzer, scanner, help bot) use whatever <strong>${label}</strong> is configured to use. To change them, edit <strong>${label}</strong> on the AI Optimization page.
      <button class="btn btn-ghost btn-sm" style="margin-left:8px;font-size:0.72rem;padding:2px 8px" onclick="tierResolutionJumpToAiOptimization('${bid}')">→ Edit on AI Optimization</button>`;
  } else {
    footer = `No primary bot configured — engine calls fall back to hardcoded DEFAULT_TIERS.`;
  }
  el.innerHTML = `<table><thead><tr><th>Tier</th><th>Name</th><th>Primary Model</th><th>Fallbacks</th><th>Cost</th><th>Source</th></tr></thead><tbody>${rows}</tbody></table>
    <div style="font-size:0.76rem;color:var(--text2);margin-top:8px;line-height:1.5">${footer}</div>`;
}

// Deep-link helper for the Tier Resolution "→ Edit on AI Optimization"
// button. Mirrors ikJumpToEmbeddingChainEditor's pattern: navigate to
// the AI Optimization page, then once it's mounted switch the bot
// selector to the primary so the operator lands on the right bot's
// Tier Definitions section without an extra click.
function tierResolutionJumpToAiOptimization(botId) {
  const navEl = document.querySelector('[data-page=ai-optimization]');
  if (navEl) nav(navEl);
  setTimeout(() => {
    if (typeof aiSwitchBot === 'function' && botId) {
      try { aiSwitchBot(botId); } catch (e) { /* non-fatal */ }
    }
  }, 100);
}

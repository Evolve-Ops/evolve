// ════════════════════════════════════════════════════════════════════════
// Page subtab: Pod Config — Config Health subtab cluster
//
// The Config Health subtab is the umbrella for several diagnostic surfaces
// that share the secMainTab top-level switcher. Extracted together because
// they all sit under the same DOM container and trade state via the
// _secMainTabActive ('audit' | 'health' | 'intents' | 'auto-memory' |
// 'permissions' | 'mcp-posture') global.
//
// Sub-clusters:
//
//   Top-level switcher + Config Health state:
//     secMainTab, _healthData, _healthExpanded, _secMainTabActive
//
//   Intents:
//     loadIntents + _renderIntents + _renderIntentRow + history toggle +
//     queued-prompt confirm + edit-reason prompt + revoke prompt
//
//   Auto-memory:
//     _amFormatBytes + _amEscape + loadAutoMemory
//
//   Permissions:
//     _permAxisBadge + loadPermissions + permToggleExpand + _permBotRow +
//     permChangeLevel + permRevokePattern + permAddPattern +
//     _permAddPatternForm + permRemoveCron (and the rest of the permissions
//     family in this contiguous block).
//
//   Config Health renderer (the heath subtab proper):
//     loadConfigHealth + _healthToggleRow + _healthAllSummary +
//     renderConfigHealth
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - api(), toast(), escHtml(), botLabel() — core/
//   - nav() / window.nav — core/router.js
//   - loadStatus() (Overview) — refreshed after permission level changes
// ════════════════════════════════════════════════════════════════════════

// Config Health
// ══════════════════════════════════════════════════════
let _healthData = null;         // {bot_id: {checks: [...], error: str|null}}
let _healthExpanded = new Set(); // check_ids whose row is expanded
let _secMainTabActive = 'audit'; // 'audit' | 'health' | 'mcp-posture' | ...

function secMainTab(tab) {
  // Graceful fallback: if something passes 'safety', show the audit tab instead.
  if (tab === 'safety') tab = 'audit';
  // 'backup' was moved to Maintenance → Backup. Old links / deep-links that
  // still pass 'backup' fall through to audit instead of a broken state.
  if (tab === 'backup') tab = 'audit';
  _secMainTabActive = tab;
  document.getElementById('sec-panel-audit').style.display          = tab === 'audit'          ? '' : 'none';
  document.getElementById('sec-panel-host').style.display           = tab === 'host'           ? '' : 'none';
  document.getElementById('sec-panel-health').style.display         = tab === 'health'         ? '' : 'none';
  document.getElementById('sec-panel-mcp-posture').style.display    = tab === 'mcp-posture'    ? '' : 'none';
  document.getElementById('sec-panel-plugin-posture').style.display = tab === 'plugin-posture' ? '' : 'none';
  document.getElementById('sec-panel-hook-posture').style.display   = tab === 'hook-posture'   ? '' : 'none';
  document.getElementById('sec-panel-content-scan').style.display   = tab === 'content-scan'   ? '' : 'none';
  document.getElementById('sec-panel-permissions').style.display    = tab === 'permissions'    ? '' : 'none';
  document.getElementById('sec-panel-intents').style.display        = tab === 'intents'        ? '' : 'none';
  document.getElementById('sec-panel-auto-memory').style.display    = tab === 'auto-memory'    ? '' : 'none';
  document.getElementById('sec-main-tab-audit').classList.toggle('active',           tab === 'audit');
  document.getElementById('sec-main-tab-host').classList.toggle('active',            tab === 'host');
  document.getElementById('sec-main-tab-health').classList.toggle('active',          tab === 'health');
  document.getElementById('sec-main-tab-mcp-posture').classList.toggle('active',     tab === 'mcp-posture');
  document.getElementById('sec-main-tab-plugin-posture').classList.toggle('active',  tab === 'plugin-posture');
  document.getElementById('sec-main-tab-hook-posture').classList.toggle('active',    tab === 'hook-posture');
  document.getElementById('sec-main-tab-content-scan').classList.toggle('active',    tab === 'content-scan');
  document.getElementById('sec-main-tab-permissions').classList.toggle('active',     tab === 'permissions');
  document.getElementById('sec-main-tab-intents').classList.toggle('active',         tab === 'intents');
  document.getElementById('sec-main-tab-auto-memory').classList.toggle('active',     tab === 'auto-memory');
  if (tab === 'audit'  && (!_auditData || !Object.keys(_auditData).length)) loadSecurityAudit();
  if (tab === 'host' && !_hostPoliciesData) loadHostPolicies();
  if (tab === 'health' && !_healthData) loadConfigHealth();
  if (tab === 'mcp-posture' && !_mcpPostureData) loadMcpPosture();
  if (tab === 'plugin-posture' && !_pluginPostureData) loadPluginPosture();
  if (tab === 'hook-posture' && !_hookPostureData) loadHookPosture();
  if (tab === 'content-scan' && !_contentScanData) loadContentScan();
  if (tab === 'permissions' && !_permissionsData) loadPermissions();
  if (tab === 'intents' && !_intentsData) loadIntents();
  if (tab === 'auto-memory' && !_autoMemoryData) loadAutoMemory();
}

// ── Host posture (FileVault and other host-level policy acceptances) ────────
//
// Surfaces each host-level check the operator can either follow (enable
// the recommended setting) or formally opt out of (recorded in
// network.json::policy_acceptances). Opting out clears the recurring
// alert immediately and prevents future ones; switching back resumes
// normal audit behavior.

let _hostPoliciesData = null;

async function loadHostPolicies(force) {
  const panel = document.getElementById('host-policies-panel');
  if (!panel) return;
  if (_hostPoliciesData && !force) {
    _renderHostPolicies(_hostPoliciesData);
    return;
  }
  panel.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  try {
    const r = await api('GET', '/api/security/host-policies');
    _hostPoliciesData = (r && r.policies) || [];
    _renderHostPolicies(_hostPoliciesData);
  } catch (e) {
    panel.innerHTML = `<div class="alert">Failed to load host policies: ${escHtml(String(e))}</div>`;
  }
}

function _renderHostPolicies(policies) {
  const panel = document.getElementById('host-policies-panel');
  if (!policies.length) {
    panel.innerHTML = '<div style="padding:12px;color:var(--text3);font-style:italic">No host-level policies defined.</div>';
    return;
  }
  panel.innerHTML = policies.map(_renderHostPolicyCard).join('');
}

function _renderHostPolicyCard(policy) {
  if (policy.id === 'machine.filevault_off') return _renderFileVaultCard(policy);
  // Unknown policy id — render a minimal generic card so the operator
  // sees it even before its descriptor lands.
  return `<div class="card" style="margin-bottom:12px">
    <div style="font-weight:600;margin-bottom:6px">${escHtml(policy.title || policy.id)}</div>
    <div style="font-size:0.78rem;color:var(--text3)">No description available.</div>
  </div>`;
}

function _renderFileVaultCard(policy) {
  const optedOut = !!policy.acceptance;
  const state = policy.state || 'unknown';
  const stateLabel = state === 'on'
    ? '<span style="color:var(--green)">✓ FileVault is on</span>'
    : state === 'off'
      ? '<span style="color:var(--red)">⚠ FileVault is off</span>'
      : state === 'indeterminate'
        ? '<span style="color:var(--yellow)">… FileVault state in progress</span>'
        : '<span style="color:var(--text3)">FileVault state unknown</span>';

  const acceptanceBlock = optedOut ? `
    <div style="background:var(--bg3);border-radius:6px;padding:10px 12px;margin:10px 0;font-size:0.82rem">
      <div style="font-weight:600;margin-bottom:4px">Recorded decision: not using FileVault</div>
      <div style="color:var(--text2)">Reason: ${escHtml(policy.acceptance.reason || '—')}</div>
      <div style="color:var(--text3);font-size:0.75rem;margin-top:4px">
        Set ${escHtml(policy.acceptance.accepted_at || '')} · by ${escHtml(policy.acceptance.accepted_by || '')}
      </div>
    </div>` : '';

  const enableSteps = !optedOut ? `
    <div style="margin:10px 0">
      <div style="font-weight:600;font-size:0.85rem;margin-bottom:6px">How to turn FileVault on</div>
      <ol style="font-size:0.82rem;line-height:1.55;padding-left:20px;margin:0;color:var(--text2)">
        <li>Open <strong>System Settings → Privacy &amp; Security → FileVault</strong> on this Mac</li>
        <li>Click <strong>Turn On…</strong> and follow the prompts</li>
        <li>Store the recovery key somewhere safe and offline — a password manager, a hardware safe, or a sealed envelope. <strong>Not on this Mac.</strong></li>
        <li>Initial encryption runs in the background and can take several hours; the Mac stays usable</li>
      </ol>
    </div>` : '';

  return `
    <div class="card" style="margin-bottom:14px">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:8px">
        <div>
          <div style="font-weight:600;font-size:1rem">FileVault disk encryption</div>
          <div style="font-size:0.8rem;margin-top:2px">${stateLabel}</div>
        </div>
      </div>

      <div style="font-size:0.85rem;line-height:1.55;color:var(--text2);margin-bottom:10px">
        <strong>What it is.</strong> FileVault is macOS's built-in full-disk
        encryption. When it's on, the contents of this Mac's drive are
        unreadable without your account password — even if someone removes
        the drive or boots from external media.
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">
        <div style="background:var(--bg2);border-radius:6px;padding:10px 12px">
          <div style="font-weight:600;font-size:0.8rem;color:var(--green);margin-bottom:4px">Benefits</div>
          <ul style="font-size:0.78rem;line-height:1.5;color:var(--text2);padding-left:18px;margin:0">
            <li>A stolen Mac is unreadable without your password</li>
            <li>Bot transcripts, API keys, and OAuth tokens stay private if the disk leaves the building (theft, repair, disposal)</li>
            <li>Negligible performance cost on modern Apple silicon — Apple turns it on by default for new Macs</li>
          </ul>
        </div>
        <div style="background:var(--bg2);border-radius:6px;padding:10px 12px">
          <div style="font-weight:600;font-size:0.8rem;color:var(--yellow);margin-bottom:4px">Tradeoffs</div>
          <ul style="font-size:0.78rem;line-height:1.5;color:var(--text2);padding-left:18px;margin:0">
            <li>You must keep the <strong>recovery key</strong> somewhere safe. If you lose both the password and the recovery key, the data is unrecoverable.</li>
            <li>Initial encryption can take several hours (runs in the background)</li>
            <li>A locked Mac can't auto-resume after a power cut until someone logs in — fine for a home pod, worth knowing for a headless rack</li>
          </ul>
        </div>
      </div>

      ${enableSteps}
      ${acceptanceBlock}

      <div style="border-top:1px solid var(--border);padding-top:10px;margin-top:6px">
        <div style="font-weight:600;font-size:0.85rem;margin-bottom:6px">Your plan</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          <label style="font-size:0.85rem;display:flex;align-items:center;gap:6px;cursor:pointer">
            <input type="radio" name="fv-plan" value="use" ${!optedOut ? 'checked' : ''} onchange="_hostPolicyChooseUse('machine.filevault_off')">
            I plan to use FileVault
          </label>
          <label style="font-size:0.85rem;display:flex;align-items:center;gap:6px;cursor:pointer">
            <input type="radio" name="fv-plan" value="opt_out" ${optedOut ? 'checked' : ''} onchange="_hostPolicyChooseOptOut('machine.filevault_off')">
            I don't plan to use FileVault
          </label>
        </div>
        <div id="fv-opt-out-form" style="display:${optedOut ? 'block' : 'none'};margin-top:10px">
          <div style="font-size:0.78rem;color:var(--text3);margin-bottom:4px">Reason (optional — helps future you remember why)</div>
          <input type="text" id="fv-opt-out-reason" class="input-w-text"
                 placeholder="e.g. Single-tenant dev mini in a locked room"
                 value="${escHtml(optedOut ? (policy.acceptance.reason || '') : '')}">
          <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
            <button class="btn btn-primary btn-sm" onclick="_hostPolicySaveOptOut('machine.filevault_off')">Save decision</button>
            ${optedOut ? `<button class="btn btn-ghost btn-sm" onclick="_hostPolicyClear('machine.filevault_off')">Clear decision</button>` : ''}
          </div>
          <div style="font-size:0.75rem;color:var(--text3);margin-top:6px">
            Saving stops the FileVault-off alert. You can switch back any time.
          </div>
        </div>
      </div>
    </div>`;
}

function _hostPolicyChooseUse(policyId) {
  // Selecting "I plan to use FileVault" implicitly clears any prior
  // opt-out. We surface a confirm-then-save flow to avoid silent loss
  // of the recorded reason.
  const form = document.getElementById('fv-opt-out-form');
  if (form) form.style.display = 'none';
  if (_hostPoliciesData && _hostPoliciesData.some(p => p.id === policyId && p.acceptance)) {
    _hostPolicyClear(policyId);
  }
}

function _hostPolicyChooseOptOut(policyId) {
  const form = document.getElementById('fv-opt-out-form');
  if (form) form.style.display = 'block';
}

async function _hostPolicySaveOptOut(policyId) {
  const reasonEl = document.getElementById('fv-opt-out-reason');
  const reason = (reasonEl && reasonEl.value || '').trim();
  try {
    const r = await api('PUT', `/api/security/host-policies/${encodeURIComponent(policyId)}`,
      { intent: 'opt_out', reason });
    _hostPoliciesData = null;
    const archived = (r && r.archived_signals) || 0;
    toast(archived
      ? `Saved. Cleared ${archived} existing alert.`
      : 'Saved.', 'ok');
    loadHostPolicies(true);
  } catch (e) {
    toast(`Failed to save: ${e}`, 'err');
  }
}

async function _hostPolicyClear(policyId) {
  try {
    await api('PUT', `/api/security/host-policies/${encodeURIComponent(policyId)}`,
      { intent: 'use' });
    _hostPoliciesData = null;
    toast('Cleared. FileVault check resumes on next audit.', 'ok');
    loadHostPolicies(true);
  } catch (e) {
    toast(`Failed to clear: ${e}`, 'err');
  }
}

// ── Intentional Deviations (Phase 4 of spec-config-intent-system-2026-05-21) ─
let _intentsData = null;

async function loadIntents(force) {
  if (_intentsData && !force) {
    _renderIntents(_intentsData);
    return;
  }
  const panel = document.getElementById('intents-panel');
  if (!panel) return;
  panel.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  try {
    const r = await api('GET', '/api/intents');
    _intentsData = (r && r.bots) || {};
    _renderIntents(_intentsData);
  } catch (e) {
    panel.innerHTML = `<div class="alert">Failed to load intents: ${escHtml(String(e))}</div>`;
  }
}

function _renderIntents(bots) {
  const panel = document.getElementById('intents-panel');
  const botIds = Object.keys(bots).sort();
  if (!botIds.length) {
    panel.innerHTML = (
      '<div style="padding:12px;color:var(--text3);font-style:italic">' +
      'No intents recorded yet. Intents land here when an operator saves a ' +
      'non-baseline value through Permissions, when an applier writes via ' +
      'a proposal, or when deploy.py infers a non-baseline exec posture.' +
      '</div>'
    );
    return;
  }
  // Surface a quick pod-wide count so the operator can see scale at a glance.
  const totalActive = botIds.reduce((acc, b) => acc + (bots[b] || []).length, 0);
  const header = (
    `<div style="margin-bottom:10px;color:var(--text3);font-size:0.78rem">` +
    `<strong>${totalActive}</strong> active intent(s) across ` +
    `<strong>${botIds.length}</strong> bot(s).</div>`
  );
  const sections = botIds.map(botId => {
    const intents = bots[botId] || [];
    if (!intents.length) {
      return (
        `<div class="card" style="margin-bottom:10px">` +
        `<div class="card-head">${escHtml(botId)}</div>` +
        `<div style="padding:8px;color:var(--text3);font-size:0.75rem">` +
        `All intents revoked. Sidecar preserved for audit history.</div></div>`
      );
    }
    const rows = intents.map(it => _renderIntentRow(botId, it)).join('');
    return (
      `<div class="card" style="margin-bottom:10px">` +
      `<div class="card-head">${escHtml(botId)} ` +
      `<span style="color:var(--text3);font-size:0.7rem;font-weight:400">` +
      `${intents.length} intent(s)</span></div>` +
      `<div>${rows}</div></div>`
    );
  }).join('');
  panel.innerHTML = header + sections;
}

function _renderIntentRow(botId, intent) {
  const setBy = intent.set_by || 'unknown';
  const setAt = intent.set_at || '';
  const reason = intent.reason || '';
  const depends = (intent.depends_on && intent.depends_on.plugin)
    ? `<span class="chip chip-muted" style="margin-left:6px" title="Intent is plugin-coupled — when the plugin is removed, this intent auto-flags as stale">plugin: ${escHtml(intent.depends_on.plugin)}</span>`
    : '';
  const valueStr = JSON.stringify(intent.value);
  const intentId = intent.id || '';
  // Phase 4.1 — queued intents are inferred-low confidence and the
  // Phase 3 inference layer flagged them for operator follow-up. Top
  // banner makes the prompt impossible to miss, plus a "Confirm" button
  // alongside the standard row actions.
  const isQueued = intent.queued === true;
  const queuedBanner = isQueued
    ? `<div style="background:var(--bg2);border-left:3px solid var(--yellow,#d29922);` +
      `padding:6px 8px;border-radius:3px;font-size:0.72rem;color:var(--text2);` +
      `display:flex;align-items:center;gap:8px;flex-wrap:wrap">` +
        `<span><strong>Low-confidence inference</strong> — the model couldn't deduce why ` +
        `this field changed. Confirm the recorded reason or replace it.</span>` +
        `<button class="btn btn-ghost btn-sm" style="margin-left:auto" ` +
        `onclick="_intentConfirmQueuedPrompt('${escHtml(botId)}', '${escHtml(intentId)}', '${escHtml(reason)}')">` +
        `Confirm reason</button>` +
      `</div>`
    : '';
  return (
    `<div class="row" style="padding:10px 8px;border-bottom:1px solid var(--border);` +
    `display:flex;flex-direction:column;gap:6px">` +
      queuedBanner +
      `<div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap">` +
        `<code style="font-size:0.85rem">${escHtml(intent.field_path)}</code>` +
        `<span style="color:var(--text3)">=</span>` +
        `<code style="font-size:0.85rem;color:var(--accent)">${escHtml(valueStr)}</code>` +
        depends +
      `</div>` +
      `<div style="color:var(--text2);font-size:0.78rem">` +
        `<em>${escHtml(reason)}</em>` +
      `</div>` +
      `<div style="color:var(--text3);font-size:0.7rem">` +
        `recorded ${escHtml(setAt)} by ${escHtml(setBy)}` +
        (intent.set_by_detail ? ` (${escHtml(intent.set_by_detail)})` : '') +
      `</div>` +
      `<div style="display:flex;gap:6px;margin-top:4px">` +
        `<button class="btn btn-ghost btn-sm" onclick="_intentToggleHistory('${escHtml(botId)}', '${escHtml(intentId)}', this)">` +
        `View history</button>` +
        `<button class="btn btn-ghost btn-sm" onclick="_intentEditReasonPrompt('${escHtml(botId)}', '${escHtml(intentId)}', '${escHtml(reason)}')">` +
        `Edit reason</button>` +
        `<button class="btn btn-ghost btn-sm" style="color:var(--red)" onclick="_intentRevokePrompt('${escHtml(botId)}', '${escHtml(intentId)}', '${escHtml(intent.field_path || '')}')">` +
        `Revoke</button>` +
      `</div>` +
      `<div id="intent-history-${escHtml(botId)}-${escHtml(intentId)}" style="display:none;margin-top:6px;` +
      `padding:8px;background:var(--bg2);border-radius:4px;font-size:0.72rem"></div>` +
    `</div>`
  );
}

async function _intentConfirmQueuedPrompt(botId, intentId, currentReason) {
  const next = window.prompt(
    `Confirm this queued intent.\n\n` +
    `The inference layer recorded this reason at low confidence and ` +
    `flagged it for follow-up:\n\n  "${currentReason}"\n\n` +
    `Leave the text below unchanged to accept the inferred reason, or ` +
    `replace it with your own. Either way, the "low-confidence" banner ` +
    `clears and the audit trail captures the acknowledgment.`,
    currentReason || '',
  );
  if (next === null) return; // cancelled
  const trimmed = next.trim();
  // Empty string acceptable — server treats it as "accept inferred reason."
  const body = trimmed && trimmed !== (currentReason || '')
    ? { new_reason: trimmed }
    : {};
  try {
    const r = await api(
      'POST',
      `/api/intents/${encodeURIComponent(botId)}/${encodeURIComponent(intentId)}/confirm-queued`,
      body,
    );
    if (r && r.ok) {
      toast('Confirmed', 'ok');
      await loadIntents(true);
    } else {
      toast(`Confirm failed: ${(r && r.error) || 'unknown'}`, 'error');
    }
  } catch (e) {
    toast(`Confirm failed: ${String(e)}`, 'error');
  }
}

function _intentToggleHistory(botId, intentId, btn) {
  const el = document.getElementById(`intent-history-${botId}-${intentId}`);
  if (!el) return;
  if (el.style.display === 'none') {
    // Render history inline from the cached data.
    const intents = (_intentsData && _intentsData[botId]) || [];
    const intent = intents.find(it => it && it.id === intentId);
    if (!intent) return;
    const history = intent.audit_history || [];
    if (!history.length) {
      el.innerHTML = '<em>No history entries recorded.</em>';
    } else {
      el.innerHTML = history.map(h => {
        const at = h.at || '';
        const actor = h.actor || '';
        const event = h.event || '';
        const extra = (() => {
          if (event === 'updated') {
            return ` <code>${escHtml(JSON.stringify(h.from_value))}</code> → <code>${escHtml(JSON.stringify(h.to_value))}</code>`;
          }
          if (event === 'reason_edited') {
            return `<br><small>reason: <em>${escHtml(h.from_reason || '')}</em> → <em>${escHtml(h.to_reason || '')}</em></small>`;
          }
          if (event === 'confirmed_queued') {
            // Phase 4.1 — operator-acknowledged a low-confidence inferred
            // intent. May or may not have replaced the reason.
            if (h.from_reason !== undefined) {
              return `<br><small>reason: <em>${escHtml(h.from_reason || '')}</em> → <em>${escHtml(h.to_reason || '')}</em></small>`;
            }
            return `<br><small>accepted inferred reason as-is</small>`;
          }
          if (event === 'set' && h.reason) {
            return `<br><small>reason: <em>${escHtml(h.reason)}</em></small>`;
          }
          return '';
        })();
        return `<div style="padding:3px 0">${escHtml(at)} <strong>${escHtml(event)}</strong> by ${escHtml(actor)}${extra}</div>`;
      }).join('');
    }
    el.style.display = '';
    if (btn) btn.textContent = 'Hide history';
  } else {
    el.style.display = 'none';
    if (btn) btn.textContent = 'View history';
  }
}

async function _intentEditReasonPrompt(botId, intentId, currentReason) {
  const next = window.prompt(
    `Edit recorded reason for ${botId} intent ${intentId}.\n\n` +
    `Updates only the reason field. The recorded value and set_by stay ` +
    `intact so the audit chain still explains who originally set the field.`,
    currentReason || '',
  );
  if (next === null) return; // cancelled
  if (!next.trim()) {
    toast('Reason cannot be empty', 'error');
    return;
  }
  try {
    const r = await api(
      'POST',
      `/api/intents/${encodeURIComponent(botId)}/${encodeURIComponent(intentId)}/edit-reason`,
      { new_reason: next.trim() },
    );
    if (r && r.ok) {
      toast('Reason updated', 'ok');
      await loadIntents(true);
    } else {
      toast(`Edit failed: ${(r && r.error) || 'unknown'}`, 'error');
    }
  } catch (e) {
    toast(`Edit failed: ${String(e)}`, 'error');
  }
}

async function _intentRevokePrompt(botId, intentId, fieldPath) {
  const ok = await confirmModal({
    body: (
      `Revoke intent for ${botId}::${fieldPath}?\n\n` +
      `This moves the intent to the sidecar's intents_archive list — the ` +
      `underlying config field is NOT changed. On the next monitor sweep, the ` +
      `deviation will be classified as actual drift and auth_drift_filler will ` +
      `emit a revert proposal you can accept or reject normally.`
    ),
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await api(
      'POST',
      `/api/intents/${encodeURIComponent(botId)}/${encodeURIComponent(intentId)}/revoke`,
      {},
    );
    if (r && r.ok) {
      toast('Intent revoked', 'ok');
      await loadIntents(true);
    } else {
      toast(`Revoke failed: ${(r && r.error) || 'unknown'}`, 'error');
    }
  } catch (e) {
    toast(`Revoke failed: ${String(e)}`, 'error');
  }
}


// ── Auto-Memory (Phase A of roadmap §2.4) ────────────────────────────────────
let _autoMemoryData = null;

function _amFormatBytes(n) {
  if (!Number.isFinite(n) || n < 0) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(2)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function _amEscape(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

async function loadAutoMemory(force) {
  const el = document.getElementById('auto-memory-panel');
  if (!el) return;
  if (force) _autoMemoryData = null;
  if (!_autoMemoryData) {
    el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  }
  try {
    _autoMemoryData = await api('GET', '/api/auto-memory/inventory');
  } catch (e) {
    el.innerHTML = `<div class="error">Inventory load failed: ${_amEscape(e && e.message || e)}</div>`;
    return;
  }
  const d = _autoMemoryData;
  const bots = d.bots || [];
  if (!bots.length) {
    el.innerHTML = `<div style="font-size:0.82rem;color:var(--text2);padding:18px">No bots in <code>network.members</code>.</div>`;
    return;
  }
  const podSummary = `
    <div style="font-size:0.78rem;color:var(--text2);margin-bottom:14px">
      Pod-wide: <strong>${d.pod_total_files || 0}</strong> file${d.pod_total_files === 1 ? '' : 's'} ·
      <strong>${_amFormatBytes(d.pod_total_bytes || 0)}</strong> markdown ·
      <strong>${_amFormatBytes(d.pod_index_db_bytes || 0)}</strong> in FTS/embedding indexes ·
      scanned at <code>${_amEscape(d.scanned_at || '')}</code>
    </div>`;
  const rows = bots.map(b => {
    if (b.error) {
      return `
        <tr>
          <td><code>${_amEscape(botLabel(b.bot_id))}</code></td>
          <td colspan="5" style="font-size:0.78rem;color:#e57373">error: ${_amEscape(b.error)}</td>
        </tr>`;
    }
    const indexCell = b.index_db_size_bytes
      ? _amFormatBytes(b.index_db_size_bytes)
      : '<span style="color:var(--text3)">—</span>';
    if (!b.memory_dir_exists) {
      return `
        <tr>
          <td><code>${_amEscape(botLabel(b.bot_id))}</code></td>
          <td colspan="4" style="font-size:0.78rem;color:var(--text3)">no <code>workspace/memory/</code> on this bot</td>
          <td style="text-align:right">${indexCell}</td>
        </tr>`;
    }
    if (!b.file_count) {
      return `
        <tr>
          <td><code>${_amEscape(botLabel(b.bot_id))}</code></td>
          <td colspan="4" style="font-size:0.78rem;color:var(--text3)">memory dir empty</td>
          <td style="text-align:right">${indexCell}</td>
        </tr>`;
    }
    return `
      <tr>
        <td><code>${_amEscape(botLabel(b.bot_id))}</code></td>
        <td style="text-align:right">${b.file_count}</td>
        <td style="text-align:right">${_amFormatBytes(b.total_bytes)}</td>
        <td style="font-size:0.72rem;color:var(--text3);white-space:nowrap">${_amEscape(b.oldest_modified_at || '—')}</td>
        <td style="font-size:0.72rem;color:var(--text3);white-space:nowrap">${_amEscape(b.newest_modified_at || '—')}</td>
        <td style="text-align:right">${indexCell}</td>
      </tr>
    `;
  }).join('');
  el.innerHTML = `
    ${podSummary}
    <table class="table">
      <thead>
        <tr>
          <th>Bot</th>
          <th style="text-align:right">Files</th>
          <th style="text-align:right">Size</th>
          <th>Oldest</th>
          <th>Newest</th>
          <th style="text-align:right">Index DB</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    <div style="font-size:0.72rem;color:var(--text3);margin-top:10px">
      Files are walked under <code>~/.openclaw/workspace/memory/</code> (recursive);
      <em>Index DB</em> is <code>~/.openclaw/memory/main.sqlite</code>, the embedding +
      FTS index OC builds over the markdown. Bots with no <code>workspace/memory/</code>
      haven't accumulated any auto-memory yet — the dir is created on first write.
    </div>
  `;
}


// ── Permissions (Phase A of spec-permission-posture) ─────────────────────────
let _permissionsData = null;
let _permissionsExpanded = new Set();  // bot ids currently expanded in card view

const _PERM_SCORE_BADGE = {
  tight:    { cls: 'badge-green',  label: 'Locked'     },
  moderate: { cls: 'badge-blue',   label: 'Supervised' },
  wide:     { cls: 'badge-yellow', label: 'Trusted'    },
  open:     { cls: 'badge-red',    label: 'Open'       },
};

const _PERM_AXIS_LABELS = {
  execution:  'Execution',
  filesystem: 'Filesystem',
  web:        'Web',
  sandbox:    'Sandbox',
  scheduled:  'Scheduled',
};

function _permAxisBadge(axis, value) {
  // Color-code each axis category: green = restricted/safe, blue = partial,
  // yellow = open-with-gate, red = unrestricted-and-risky, gray = absent.
  const map = {
    'deny':                 'badge-green',
    'allowlist':            'badge-green',
    'full+ask-always':      'badge-blue',
    'full+ask-on-miss':     'badge-yellow',
    'full+ask-off':         'badge-red',
    'unset':                'badge-gray',
    'workspace-only':       'badge-green',
    'unrestricted':         'badge-yellow',
    'off':                  'badge-green',
    'partial':              'badge-blue',
    'open':                 'badge-yellow',
    'on':                   'badge-green',
    'none':                 'badge-yellow',
    'no-cron':              'badge-gray',
    'no-agent-turns':       'badge-green',
    'capped':               'badge-green',
    'uncapped-agent-turns': 'badge-yellow',
  };
  const cls = map[value] || 'badge-gray';
  return `<span class="badge ${cls}" title="${escHtml(_PERM_AXIS_LABELS[axis] || axis)}: ${escHtml(value)}">${escHtml(value)}</span>`;
}

async function loadPermissions(forceRescan) {
  const el = document.getElementById('permissions-panel');
  if (!el) return;
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  if (forceRescan) {
    // Triggers the audit-equivalent monitor cycle: emits Signals + sweeps stale.
    try { await api('POST', '/api/permissions/scan'); } catch (_e) { /* tolerated */ }
  }
  try {
    _permissionsData = await api('GET', '/api/permissions/inventory');
  } catch (e) {
    el.innerHTML = `<div class="empty">Failed to load: ${escHtml(String(e))}</div>`;
    return;
  }
  renderPermissions();
  loadAutonomy();  // fire-and-forget; fills #autonomy-section when ready
}

function permToggleExpand(botId) {
  if (_permissionsExpanded.has(botId)) _permissionsExpanded.delete(botId);
  else _permissionsExpanded.add(botId);
  renderPermissions();
}

function _permBotRow(botId, inv) {
  if (inv.error) {
    return `<tr><td><strong>${escHtml(botLabel(botId))}</strong></td>
      <td colspan="6" style="color:var(--text3);font-size:0.78rem">${escHtml(inv.error)}</td>
      <td></td></tr>`;
  }
  const cp = inv.composite_posture || {};
  const axes = cp.axes || {};
  const score = cp.score || 'unset';
  const scoreCfg = _PERM_SCORE_BADGE[score] || { cls: 'badge-gray', label: score };
  const denyApprovals = (inv.denylist_matches || {}).approvals || [];
  const denyCron = (inv.denylist_matches || {}).cron || [];
  const denyTotal = denyApprovals.length + denyCron.length;
  const expanded = _permissionsExpanded.has(botId);

  return `<tr>
    <td><strong>${escHtml(botLabel(botId))}</strong></td>
    <td><span class="badge ${scoreCfg.cls}" title="${escHtml(cp.rationale || '')}">${escHtml(scoreCfg.label)}</span></td>
    <td>${_permAxisBadge('execution',  axes.execution  || 'unset')}</td>
    <td>${_permAxisBadge('filesystem', axes.filesystem || 'unset')}</td>
    <td>${_permAxisBadge('web',        axes.web        || 'unset')}</td>
    <td>${_permAxisBadge('sandbox',    axes.sandbox    || 'unset')}</td>
    <td>${_permAxisBadge('scheduled',  axes.scheduled  || 'unset')}</td>
    <td style="text-align:center">${denyTotal ? `<span class="badge badge-red" title="${escHtml(denyTotal)} denylist match${denyTotal===1?'':'es'}">⚠ ${denyTotal}</span>` : '<span style="color:var(--text3)">·</span>'}</td>
    <td style="text-align:right"><button class="btn btn-ghost btn-sm" onclick="permToggleExpand('${escHtml(botId)}')">${expanded ? 'Hide' : 'Details'}</button></td>
  </tr>`;
}

// Field-sets corresponding to each posture level. Clicking a "Change level"
// button submits an UpdatePermissionConfig proposal with the matching fields.
// "Open" is intentionally omitted — operators wanting an open posture must
// edit fields manually so the choice is deliberate.
// Sandbox is intentionally omitted from these field sets — OC's config
// schema has no top-level `sandbox` key (the valid path is
// agents.defaults.sandbox.mode) and the applier rejects unknown paths.
// Until sandbox posture is wired against the real OC schema path, "tight"
// means allowlist + ask=on-miss + workspaceOnly (a strict superset of
// "moderate" so the labels remain monotonic).
const _PERM_LEVEL_FIELDS = {
  tight: {
    'tools.exec.security': 'allowlist',
    'tools.exec.ask':      'on-miss',
    'tools.fs.workspaceOnly': true,
  },
  moderate: {
    'tools.exec.security': 'allowlist',
    'tools.exec.ask':      'on-miss',
  },
  wide: {
    'tools.exec.security': 'full',
    'tools.exec.ask':      'on-miss',
  },
};

async function permChangeLevel(botId, level) {
  const fields = _PERM_LEVEL_FIELDS[level];
  if (!fields) return;
  const label = _PERM_SCORE_BADGE[level] ? _PERM_SCORE_BADGE[level].label : level;
  if (!await confirmModal({ body: `Change ${botLabel(botId)} permission level to '${label}'?`, danger: true })) return;
  try {
    const resp = await api('POST', '/api/permissions/config', {
      bot_id: botId,
      fields,
      summary: `Change ${botId} permission level to '${label}'`,
    });
    if (handleOperatorProposalResult(`Permission level set to '${label}' on ${botLabel(botId)}`, resp)) {
      loadPermissions(true);
    }
  } catch (e) {
    toast(`Request failed: ${e}`, 'err');
  }
}

async function permRevokePattern(botId, agentId, pattern) {
  if (!await confirmModal({ body: `Revoke exec-approval ${pattern} from ${botLabel(botId)}?`, danger: true })) return;
  try {
    const resp = await api('POST', '/api/permissions/approval', {
      bot_id: botId, agent_id: agentId, operation: 'revoke', pattern, scope: 'agent',
    });
    if (handleOperatorProposalResult(`Revoked ${pattern} on ${botId}`, resp)) {
      loadPermissions(true);
    }
  } catch (e) { toast(`Request failed: ${e}`, 'err'); }
}

async function permAddPattern(botId) {
  const input = document.getElementById(`perm-add-pattern-${botId}`);
  if (!input) return;
  const pattern = (input.value || '').trim();
  if (!pattern) { toast('Pattern is required.', 'err'); return; }
  try {
    const resp = await api('POST', '/api/permissions/approval', {
      bot_id: botId, operation: 'add', pattern, agent_id: 'main', scope: 'agent',
    });
    if (handleOperatorProposalResult(`Added exec-approval ${pattern} on ${botId}`, resp)) {
      input.value = '';
      loadPermissions(true);
    }
  } catch (e) { toast(`Request failed: ${e}`, 'err'); }
}

function _permAddPatternForm(botId) {
  return `<div style="margin-top:10px;display:flex;gap:6px;align-items:center">
    <input type="text" id="perm-add-pattern-${escHtml(botId)}"
           placeholder="e.g. python3 scripts/tasks.py list"
           style="flex:1;font-family:monospace;font-size:0.78rem;padding:4px 8px;border:1px solid var(--border);border-radius:4px;background:var(--bg2);color:var(--text)">
    <button class="btn btn-ghost btn-sm" onclick="permAddPattern('${escHtml(botId)}')">Add pattern</button>
  </div>`;
}

async function permRemoveCron(botId, jobId, jobName) {
  if (!await confirmModal({ body: `Remove cron job '${jobName}' from ${botLabel(botId)}?`, danger: true })) return;
  try {
    const resp = await api('POST', '/api/permissions/cron/remove', {
      bot_id: botId, job_id: jobId,
    });
    if (handleOperatorProposalResult(`Removed cron '${jobName}' from ${botId}`, resp)) {
      loadPermissions(true);
    }
  } catch (e) { toast(`Request failed: ${e}`, 'err'); }
}

async function permEditCaps(botId, jobId, currentJob) {
  // Browser-native prompts — sufficient for the v1 "Just add caps" workflow.
  // A richer editor can land later when there are more cron-job mutation actions.
  const turnsStr = prompt(`Set maxTurns for cron job '${currentJob.name || jobId}' on ${botId}:`, '20');
  if (turnsStr === null) return;
  const budgetStr = prompt(`Set maxBudgetUsd for cron job '${currentJob.name || jobId}':`, '1.00');
  if (budgetStr === null) return;
  const maxTurns = parseInt(turnsStr, 10);
  const maxBudgetUsd = parseFloat(budgetStr);
  if (!(maxTurns > 0) || !(maxBudgetUsd > 0)) {
    toast('Both maxTurns and maxBudgetUsd must be positive numbers.', 'err');
    return;
  }
  // Construct the new job by merging caps into payload
  const newJob = JSON.parse(JSON.stringify(currentJob));
  newJob.payload = newJob.payload || {};
  newJob.payload.maxTurns = maxTurns;
  newJob.payload.maxBudgetUsd = maxBudgetUsd;
  // Clear the inventory-only fields the API doesn't expect
  delete newJob.has_turn_cap;
  delete newJob.has_budget_cap;
  delete newJob.payload_summary;
  delete newJob.signature;
  try {
    const resp = await api('POST', '/api/permissions/cron/upsert', {
      bot_id: botId, job: newJob,
    });
    if (handleOperatorProposalResult(`Updated cron caps on ${botId}`, resp)) {
      loadPermissions(true);
    }
  } catch (e) { toast(`Request failed: ${e}`, 'err'); }
}

function _permLevelPicker(botId, currentScore) {
  const opts = ['tight', 'moderate', 'wide'].map(lvl => {
    const cfg = _PERM_SCORE_BADGE[lvl];
    const active = lvl === currentScore;
    // Active uses btn-primary so the selected level is clearly readable
    // when disabled (the base .btn class has no background/color and the
    // disabled state used to render dark-on-dark in the dark theme).
    const cls = active ? 'btn-primary' : 'btn-ghost';
    return `<button class="btn ${cls} btn-sm"
                   onclick="permChangeLevel('${escHtml(botId)}', '${lvl}')"
                   ${active ? 'disabled' : ''}
                   title="${active ? 'Already at this level' : `Change to ${cfg.label}`}">${cfg.label}</button>`;
  }).join(' ');
  return `<div style="margin-bottom:14px;padding:10px;background:var(--bg-soft,rgba(0,0,0,0.02));border-radius:6px">
    <div style="font-size:0.82rem;color:var(--text2);margin-bottom:6px">
      <strong>Change level</strong> — applies on click; the change is recorded in the audit log
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">${opts}</div>
  </div>`;
}

function _permBotCard(botId, inv) {
  if (inv.error) return '';
  const cp = inv.composite_posture || {};
  const pc = inv.permission_config || {};
  const ea = inv.exec_approvals || {};
  const si = inv.scheduled_invocations || {};
  const dm = inv.denylist_matches || { approvals: [], cron: [] };

  // Permission-config field table
  const fields = pc.fields || {};
  const fieldRows = Object.keys(fields).sort().map(k => {
    const v = fields[k];
    const display = (v === null || v === undefined) ? '<span style="color:var(--text3)">unset</span>'
      : (typeof v === 'object') ? `<code>${escHtml(JSON.stringify(v))}</code>`
      : `<code>${escHtml(String(v))}</code>`;
    return `<tr><td style="font-family:monospace;font-size:0.78rem">${escHtml(k)}</td><td>${display}</td></tr>`;
  }).join('');

  // Exec approvals
  let approvalsBlock = '';
  if (ea.read_error) {
    approvalsBlock = `<div style="color:var(--text3);font-size:0.78rem">exec-approvals.json: ${escHtml(ea.read_error)}</div>`;
  } else if (!ea.present) {
    approvalsBlock = `
      <div style="color:var(--text3);font-size:0.78rem;margin-bottom:6px">No exec-approvals.json yet — bot has not accumulated runtime approvals.</div>
      ${_permAddPatternForm(botId)}`;
  } else {
    const totalAgents = (ea.agents || []).length;
    const totalPatterns = (ea.agents || []).reduce((s, a) => s + (a.count || 0), 0);
    const agentBlocks = (ea.agents || []).map(a => {
      const patternRows = (a.patterns || []).map(p => `
        <li style="font-family:monospace;font-size:0.78rem;display:flex;align-items:center;gap:8px">
          <span style="flex:1">${escHtml(p)}</span>
          <button class="btn btn-ghost btn-sm" style="font-size:0.7rem;padding:2px 6px"
                  onclick="permRevokePattern('${escHtml(botId)}', '${escHtml(a.agent_id)}', ${JSON.stringify(p).replace(/"/g, '&quot;')})">revoke</button>
        </li>`).join('');
      return `<div style="margin-top:8px">
        <div style="font-size:0.82rem"><strong>${escHtml(a.agent_id)}</strong> — ${a.count} pattern${a.count===1?'':'s'}</div>
        ${patternRows ? `<ul style="margin:4px 0 0 0;padding:0;list-style:none">${patternRows}</ul>` : ''}
      </div>`;
    }).join('');
    approvalsBlock = `
      <div style="font-size:0.82rem;color:var(--text2);margin-bottom:6px">
        <strong>${ea.defaults_count}</strong> default${ea.defaults_count===1?'':'s'} ·
        <strong>${totalAgents}</strong> agent${totalAgents===1?'':'s'} ·
        <strong>${totalPatterns}</strong> total approved pattern${totalPatterns===1?'':'s'}
      </div>${agentBlocks}
      ${_permAddPatternForm(botId)}`;
  }

  // Cron jobs
  let cronBlock = '';
  if (si.read_error) {
    cronBlock = `<div style="color:var(--text3);font-size:0.78rem">cron/jobs.json: ${escHtml(si.read_error)}</div>`;
  } else if (!si.present || !(si.jobs || []).length) {
    cronBlock = '<div style="color:var(--text3);font-size:0.78rem">No cron jobs.</div>';
  } else {
    const cronRows = (si.jobs || []).map(j => {
      const isAgentTurn = j.payload_kind === 'agentTurn';
      const capsCell = isAgentTurn
        ? (j.has_turn_cap && j.has_budget_cap
            ? '<span class="badge badge-green">capped</span>'
            : '<span class="badge badge-yellow" title="agentTurn without turn+budget cap">uncapped</span>')
        : '<span style="color:var(--text3)">n/a</span>';
      const jid = j.id || '';
      const capsAction = (isAgentTurn && !(j.has_turn_cap && j.has_budget_cap))
        ? `<button class="btn btn-ghost btn-sm" style="font-size:0.7rem;padding:2px 6px"
                  onclick="permEditCaps('${escHtml(botId)}', '${escHtml(jid)}', ${JSON.stringify(j).replace(/"/g, '&quot;')})">add caps</button>`
        : '';
      const removeAction = jid
        ? `<button class="btn btn-ghost btn-sm" style="font-size:0.7rem;padding:2px 6px"
                  onclick="permRemoveCron('${escHtml(botId)}', '${escHtml(jid)}', '${escHtml(j.name || jid)}')">remove</button>`
        : '';
      return `<tr>
        <td style="font-size:0.82rem">${escHtml(j.name || j.id || '?')}</td>
        <td style="font-size:0.78rem;color:var(--text2)">${escHtml(j.schedule_kind)}</td>
        <td style="font-size:0.78rem;color:var(--text2)">${escHtml(j.payload_kind)}</td>
        <td>${capsCell}</td>
        <td style="font-size:0.78rem;color:var(--text2);font-family:monospace">${escHtml(j.payload_summary || '')}</td>
        <td style="text-align:right;white-space:nowrap">${capsAction} ${removeAction}</td>
      </tr>`;
    }).join('');
    cronBlock = `<div style="overflow-x:auto"><table class="data-table" style="width:100%">
      <thead><tr><th>Name</th><th>Schedule</th><th>Payload</th><th>Caps</th><th>Summary</th><th></th></tr></thead>
      <tbody>${cronRows}</tbody>
    </table></div>`;
  }

  // Denylist matches
  const denyTotal = dm.approvals.length + dm.cron.length;
  let denyBlock = '';
  if (denyTotal) {
    const rows = [
      ...dm.approvals.map(m => ({...m, kind: 'approval'})),
      ...dm.cron.map(m => ({...m, kind: 'cron'})),
    ].map(m => `<tr>
      <td><span class="badge badge-red">${escHtml(m.kind)}</span></td>
      <td style="font-family:monospace;font-size:0.78rem">${escHtml(m.pattern)}</td>
      <td style="font-family:monospace;font-size:0.72rem;color:var(--text3)">${escHtml(m.rule)}</td>
      <td style="font-size:0.78rem;color:var(--text3)">${escHtml(m.context)}</td>
    </tr>`).join('');
    denyBlock = `<div style="overflow-x:auto"><table class="data-table" style="width:100%">
      <thead><tr><th>Kind</th><th>Pattern</th><th>Rule</th><th>Where</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
  } else {
    denyBlock = '<div style="color:var(--text3);font-size:0.78rem">No denylist matches.</div>';
  }

  return `<tr class="perm-detail-row"><td colspan="9" class="resp-table-fullspan" style="background:var(--bg-soft,rgba(0,0,0,0.02));padding:14px 18px">
    <div style="font-size:0.78rem;color:var(--text3);margin-bottom:10px">
      ${escHtml(cp.rationale || '')} · observed ${escHtml(inv.observed_at || '')}
    </div>

    ${_permLevelPicker(botId, cp.score || '')}

    <div style="font-weight:600;font-size:0.85rem;margin-bottom:6px">Permission config (openclaw.json)</div>
    ${pc.read_error
      ? `<div style="color:var(--text3);font-size:0.78rem">${escHtml(pc.read_error)}</div>`
      : `<table class="data-table" style="width:100%;margin-bottom:14px"><tbody>${fieldRows}</tbody></table>`}

    <div style="font-weight:600;font-size:0.85rem;margin-top:14px;margin-bottom:6px">Approved commands (exec-approvals.json)</div>
    ${approvalsBlock}

    <div style="font-weight:600;font-size:0.85rem;margin-top:14px;margin-bottom:6px">Scheduled jobs (cron/jobs.json)</div>
    ${cronBlock}

    <div style="font-weight:600;font-size:0.85rem;margin-top:14px;margin-bottom:6px">Denylist matches</div>
    ${denyBlock}
  </td></tr>`;
}

function renderPermissions() {
  const el = document.getElementById('permissions-panel');
  if (!el || !_permissionsData) return;
  const bots = orderedBotIds(_permissionsData.bots);
  if (!bots.length) {
    el.innerHTML = '<div class="empty">No bots in network.json.</div>';
    return;
  }

  // Top summary
  const scoreCounts = { tight: 0, moderate: 0, wide: 0, open: 0 };
  let totalUncappedCron = 0, totalDenyMatches = 0, totalApprovedPatterns = 0;
  for (const bid of bots) {
    const inv = _permissionsData.bots[bid];
    if (!inv || inv.error) continue;
    const s = (inv.composite_posture || {}).score;
    if (s && s in scoreCounts) scoreCounts[s]++;
    for (const j of (inv.scheduled_invocations || {}).jobs || []) {
      if (j.payload_kind === 'agentTurn' && !(j.has_turn_cap && j.has_budget_cap)) totalUncappedCron++;
    }
    const dm = inv.denylist_matches || {};
    totalDenyMatches += (dm.approvals || []).length + (dm.cron || []).length;
    for (const a of (inv.exec_approvals || {}).agents || []) totalApprovedPatterns += (a.count || 0);
  }

  const summary = `<div style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:14px;font-size:0.85rem">
    <div><strong>${bots.length}</strong> bot${bots.length===1?'':'s'}</div>
    <div style="color:${scoreCounts.open ? 'var(--red)' : 'var(--text2)'}"><strong>${scoreCounts.open}</strong> open</div>
    <div style="color:${scoreCounts.wide ? 'var(--yellow)' : 'var(--text2)'}"><strong>${scoreCounts.wide}</strong> trusted</div>
    <div><strong>${scoreCounts.moderate}</strong> supervised</div>
    <div style="color:${scoreCounts.tight ? 'var(--green)' : 'var(--text2)'}"><strong>${scoreCounts.tight}</strong> locked</div>
    <div style="color:${totalDenyMatches ? 'var(--red)' : 'var(--text2)'}"><strong>${totalDenyMatches}</strong> denylist match${totalDenyMatches===1?'':'es'}</div>
    <div style="color:${totalUncappedCron ? 'var(--yellow)' : 'var(--text2)'}"><strong>${totalUncappedCron}</strong> uncapped agent-turn cron</div>
    <div><strong>${totalApprovedPatterns}</strong> approved pattern${totalApprovedPatterns===1?'':'s'}</div>
  </div>`;

  // Matrix rows + optional detail rows
  const rows = bots.sort().flatMap(bid => {
    const inv = _permissionsData.bots[bid] || { bot_id: bid, error: 'no data' };
    const out = [_permBotRow(bid, inv)];
    if (_permissionsExpanded.has(bid) && !inv.error) out.push(_permBotCard(bid, inv));
    return out;
  }).join('');

  el.innerHTML = `${summary}
    <div class="resp-table-wrap"><table class="resp-table data-table" style="width:100%">
      <thead><tr>
        <th>Bot</th>
        <th>Level</th>
        <th>Execution</th>
        <th>Filesystem</th>
        <th>Web</th>
        <th>Sandbox</th>
        <th>Scheduled</th>
        <th style="text-align:center">Deny</th>
        <th></th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    <div style="margin-top:10px;font-size:0.72rem;color:var(--text3)">
      Levels: <span class="badge badge-green">Locked</span> only allowlisted commands run ·
      <span class="badge badge-blue">Supervised</span> one narrowing axis (allowlist, sandbox, or fs-scope) ·
      <span class="badge badge-yellow">Trusted</span> full exec + ask-on-miss (today's pod default) ·
      <span class="badge badge-red">Open</span> full + ask-off + no sandbox.
    </div>
    <div id="autonomy-section" style="margin-top:22px"></div>`;
  _respTableLabelize(el);
  // Refill the autonomy section (innerHTML above wiped it). No-op until
  // its data has loaded; loadPermissions kicks the fetch in parallel.
  renderAutonomy();
}

// ── Autonomy (spec-autonomy-ladder-2026-06-10 §4.1) ──────────────────────────
// One row per ladder-eligible integration per bot. Operator labels only
// ("Drafts only" / "Asks first" / "Acts within limits") — rung keys stay
// in the API payloads. Bots with no eligible integrations render nothing.
let _autonomyData = null;
const _autonomyExpanded = new Set();   // `${botId}::${integrationId}`
const _autonomyRulesOpen = new Set();  // rows with the limits form open

const _AUT_LEVEL_BADGE = {
  draft_only:              'badge-green',
  act_with_approval:       'badge-blue',
  autonomous_within_rules: 'badge-yellow',
};

async function loadAutonomy() {
  const el = document.getElementById('autonomy-section');
  if (!el) return;
  try {
    _autonomyData = await api('GET', '/api/autonomy/inventory');
  } catch (_e) {
    _autonomyData = null;
    el.innerHTML = '';
    return;
  }
  renderAutonomy();
}

function _autKey(botId, iid) { return `${botId}::${iid}`; }

function _autRow(botId, iid) {
  const bot = ((_autonomyData || {}).bots || {})[botId];
  if (!bot) return null;
  return (bot.integrations || []).find(r => r.integration_id === iid) || null;
}

function autToggleExpand(botId, iid) {
  const k = _autKey(botId, iid);
  if (_autonomyExpanded.has(k)) { _autonomyExpanded.delete(k); _autonomyRulesOpen.delete(k); }
  else _autonomyExpanded.add(k);
  renderAutonomy();
}

function _autFmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function _autActorText(actor) {
  if (actor === 'operator_ui') return 'you';
  if (actor === 'shipped_default') return 'install default';
  if (actor === 'backfill_inferred') return 'found as-is';
  if (actor === 'primary_bot') return 'you, via your assistant';
  // Phase B provenance actors carry an id suffix (proposal:<id> /
  // auto_demotion:<signal_id>) — operator copy drops the internals.
  if (actor && actor.startsWith('proposal:')) return 'an approved suggestion';
  if (actor && actor.startsWith('auto_demotion:')) return 'automatic safety step-down';
  return actor || 'unknown';
}

function _autSetByText(row) {
  const actor = (row.set_by || {}).actor || '';
  if (actor === 'operator_ui') return 'set by you';
  if (actor === 'shipped_default') return 'set at install';
  if (actor === 'backfill_inferred') return 'as found — not confirmed yet';
  if (actor === 'primary_bot') return 'set by you, via your assistant';
  if (actor.startsWith('proposal:')) return 'set by an approved suggestion';
  if (actor.startsWith('auto_demotion:')) return 'stepped down automatically after an incident';
  return actor ? `set by ${escHtml(actor)}` : '';
}

function _autStatusBadge(row) {
  if (!row.in_sync) {
    return '<span class="badge badge-red" title="What the bot can actually do no longer matches this setting. Re-apply below.">Out of sync</span>';
  }
  if (row.unconfirmed) {
    return '<span class="badge badge-yellow" title="Observed from the current configuration — nobody has confirmed this is intended yet.">Not confirmed</span>';
  }
  if (row.enforcement_mode === 'mechanical') {
    return '<span class="badge badge-green" title="Blocked at the tool level — the bot cannot do more even if asked to.">Enforced</span>';
  }
  return '<span class="badge badge-blue" title="The bot is instructed to behave this way and monitored for it. It is not a hard technical block.">Instructed and monitored</span>';
}

function _autHistoryBlock(row) {
  const hist = (row.history || []).slice(-6).reverse();
  if (!hist.length) return '';
  const items = hist.map(h => {
    const from = h.from ? (h.from in _AUT_LEVEL_BADGE ? _autLevelLabel(h.from) : h.from) : '—';
    const to = _autLevelLabel(h.to);
    return `<div>${escHtml(_autFmtDate(h.at))} · ${escHtml(from)} → <strong>${escHtml(to)}</strong> · ${escHtml(_autActorText(h.actor))}${h.note ? ` · ${escHtml(h.note)}` : ''}</div>`;
  }).join('');
  return `<div style="font-weight:600;font-size:0.85rem;margin-top:14px;margin-bottom:6px">History</div>
    <div style="font-size:0.75rem;color:var(--text2);display:flex;flex-direction:column;gap:4px">${items}</div>`;
}

function _autLevelLabel(rung) {
  const row = { draft_only: 'Drafts only', act_with_approval: 'Asks first', autonomous_within_rules: 'Acts within limits' };
  return row[rung] || rung;
}

function _autRulesSummary(row) {
  const rules = row.rules || {};
  if (!Object.keys(rules).length) return '';
  const bits = [];
  if (Array.isArray(rules.reach_allow) && rules.reach_allow.length) bits.push(`May write to: ${rules.reach_allow.map(escHtml).join(', ')}`);
  if (Array.isArray(rules.scope_allow) && rules.scope_allow.length) bits.push(`May touch: ${rules.scope_allow.map(escHtml).join(', ')}`);
  if (rules.actions_per_day) bits.push(`At most ${escHtml(String(rules.actions_per_day))} actions per day`);
  if (Array.isArray(rules.never) && rules.never.length) bits.push(`Never: ${rules.never.map(escHtml).join(', ')}`);
  if (!bits.length) return '';
  return `<div style="font-weight:600;font-size:0.85rem;margin-top:14px;margin-bottom:6px">Limits</div>
    <div style="font-size:0.78rem;color:var(--text2);display:flex;flex-direction:column;gap:4px">${bits.map(b => `<div>${b}</div>`).join('')}</div>`;
}

function _autRulesForm(botId, iid, row) {
  // Inline limits form for promotion to "Acts within limits" (§3.1: the
  // confirmation states what changes and requires the limits up front).
  const sfx = `${escHtml(botId)}-${escHtml(iid)}`;
  return `<div style="margin-top:14px;padding:12px;background:var(--bg3);border-radius:8px">
    <div style="font-weight:600;font-size:0.85rem;margin-bottom:6px">Set limits before allowing this</div>
    <div style="font-size:0.78rem;color:var(--text2);margin-bottom:10px">${escHtml(row.promote ? row.promote.consequence : '')}</div>
    <div style="margin-bottom:10px">
      <label style="display:block;font-size:0.75rem;color:var(--text2);margin-bottom:4px">Allowed recipients (comma-separated addresses or @domains) <span style="color:var(--accent)">*</span></label>
      <input type="text" class="input-w-lg" id="aut-reach-${sfx}" placeholder="alex@example.com, @example-company.com">
    </div>
    <div style="margin-bottom:12px">
      <label style="display:block;font-size:0.75rem;color:var(--text2);margin-bottom:4px">Most actions per day <span style="color:var(--accent)">*</span></label>
      <input type="number" min="1" class="input-w-sm" id="aut-perday-${sfx}" value="20">
    </div>
    <div style="display:flex;gap:8px">
      <button class="btn btn-green btn-sm" onclick="autSubmitRules('${escHtml(botId)}','${escHtml(iid)}')">Allow within these limits</button>
      <button class="btn btn-ghost btn-sm" onclick="autCancelRules('${escHtml(botId)}','${escHtml(iid)}')">Cancel</button>
    </div>
  </div>`;
}

function _autExpandedCard(botId, row) {
  const k = _autKey(botId, row.integration_id);
  const statusLine = row.in_sync
    ? (row.enforcement_mode === 'mechanical'
        ? 'This is blocked at the tool level — the bot cannot do more even if asked to.'
        : 'The bot is instructed to behave this way and monitored for it. It is not a hard technical block.')
    : 'What the bot can actually do no longer matches this setting (its configuration changed out-of-band). Re-apply the setting below, or see the Alerts page for detail.';
  const unconfirmedHint = row.unconfirmed
    ? `<div class="alert alert-warn" style="margin-top:10px">This reflects how the bot was already set up — nobody has confirmed it yet. Keep it, or restrict it.</div>`
    : '';
  const reapply = !row.in_sync
    ? `<button class="btn btn-ghost btn-sm" style="margin-top:10px" onclick="autConfirmCurrent('${escHtml(botId)}','${escHtml(row.integration_id)}')">Re-apply this setting</button>`
    : '';
  const rulesForm = _autonomyRulesOpen.has(k) && row.promote && row.promote.requires_rules
    ? _autRulesForm(botId, row.integration_id, row)
    : '';
  return `<tr><td colspan="6" class="resp-table-fullspan" style="background:var(--bg3);padding:14px 18px">
    <div style="font-weight:600;font-size:0.85rem;margin-bottom:6px">What "${escHtml(row.rung_label)}" means here</div>
    <div style="font-size:0.78rem;color:var(--text2);max-width:64ch">${escHtml(row.rung_meaning || '')}</div>
    <div style="font-size:0.78rem;color:var(--text2);margin-top:8px;max-width:64ch">${_autStatusBadge(row)} <span style="margin-left:6px">${escHtml(statusLine)}</span></div>
    ${unconfirmedHint}
    ${reapply}
    ${_autRulesSummary(row)}
    ${rulesForm}
    ${_autHistoryBlock(row)}
  </td></tr>`;
}

function _autActionButtons(botId, row) {
  const b = [];
  const id = `'${escHtml(botId)}','${escHtml(row.integration_id)}'`;
  if (row.unconfirmed) {
    b.push(`<button class="btn btn-ghost btn-sm" onclick="autConfirmCurrent(${id})" title="Record the current behavior as deliberate">Keep</button>`);
  }
  if (row.promote) {
    b.push(`<button class="btn btn-ghost btn-sm" onclick="autPromote(${id})">Allow more</button>`);
  }
  if (row.demote) {
    b.push(`<button class="btn btn-ghost btn-sm" onclick="autDemote(${id})" title="Takes effect within seconds — no confirmation">Restrict</button>`);
  }
  return b.join(' ');
}

function renderAutonomy() {
  const el = document.getElementById('autonomy-section');
  if (!el) return;
  const bots = _autonomyData ? orderedBotIds(_autonomyData.bots || {}) : [];
  if (!bots.length) {
    // No ladder-eligible integrations anywhere → render nothing at all
    // (no empty section, no dead affordances — spec §4.1).
    el.innerHTML = '';
    return;
  }
  const rows = bots.flatMap(botId => {
    const integ = (_autonomyData.bots[botId] || {}).integrations || [];
    return integ.flatMap(row => {
      if (row.error) {
        return [`<tr><td data-label="Bot"><strong>${escHtml(botLabel(botId))}</strong></td>
          <td colspan="5" style="color:var(--text3);font-size:0.78rem">${escHtml(row.error)}</td></tr>`];
      }
      const k = _autKey(botId, row.integration_id);
      const expanded = _autonomyExpanded.has(k);
      const levelCls = _AUT_LEVEL_BADGE[row.rung] || 'badge-gray';
      const out = [`<tr>
        <td data-label="Bot"><strong>${escHtml(botLabel(botId))}</strong></td>
        <td data-label="Integration">${escHtml(row.integration_label)}</td>
        <td data-label="Level"><span class="badge ${levelCls}" title="${escHtml(row.rung_meaning || '')}">${escHtml(row.rung_label)}</span></td>
        <td data-label="Status">${_autStatusBadge(row)}</td>
        <td data-label="Since" style="font-size:0.75rem;color:var(--text2);white-space:nowrap">${escHtml(_autFmtDate(row.set_at))}${row.set_at ? ' · ' : ''}${_autSetByText(row)}</td>
        <td data-label="" style="text-align:right;white-space:nowrap">${_autActionButtons(botId, row)}
          <button class="btn btn-ghost btn-sm" onclick="autToggleExpand('${escHtml(botId)}','${escHtml(row.integration_id)}')">${expanded ? 'Hide' : 'Details'}</button></td>
      </tr>`];
      if (expanded) out.push(_autExpandedCard(botId, row));
      return out;
    });
  }).join('');

  el.innerHTML = `
    <h2 style="margin-bottom:4px">Autonomy</h2>
    <div style="font-size:0.78rem;color:var(--text2);margin-bottom:10px;max-width:64ch">
      What each bot may do on its connected accounts without you.
      Allowing more always asks you to confirm; restricting is one click
      and takes effect within seconds.
    </div>
    <div class="resp-table-wrap"><table class="resp-table data-table" style="width:100%">
      <thead><tr>
        <th>Bot</th>
        <th>Integration</th>
        <th>Level</th>
        <th>Status</th>
        <th>Since</th>
        <th></th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
  _respTableLabelize(el);
}

async function _autPost(botId, iid, body, okMsg) {
  try {
    const resp = await api('POST',
      `/api/autonomy/${encodeURIComponent(botId)}/${encodeURIComponent(iid)}`, body);
    if (!resp || resp.ok !== true) {
      if (resp && resp.stale) {
        toast('That setting was changed elsewhere just now — reloading.', 'err');
        loadAutonomy();
        return;
      }
      toast(`Change failed: ${(resp && resp.error) || 'unknown error'}`, 'err');
      return;
    }
    if (resp.warning) toast(resp.warning, 'err');
    else toast(okMsg, 'ok');
    loadAutonomy();
  } catch (e) {
    toast(`Request failed: ${e}`, 'err');
  }
}

async function autPromote(botId, iid) {
  const row = _autRow(botId, iid);
  if (!row || !row.promote) return;
  if (row.promote.requires_rules) {
    // Promotion to "Acts within limits" needs the limits up front —
    // open the inline form instead of a bare confirm().
    const k = _autKey(botId, iid);
    _autonomyExpanded.add(k);
    _autonomyRulesOpen.add(k);
    renderAutonomy();
    return;
  }
  if (!await confirmModal(`${row.promote.consequence}\n\nAllow ${botLabel(botId)} more for ${row.integration_label}?`)) return;
  _autPost(botId, iid,
    { rung: row.promote.rung, expected_current_rung: row.rung },
    `${row.integration_label} on ${botLabel(botId)} set to "${row.promote.rung_label}"`);
}

function autDemote(botId, iid) {
  // One click, no confirmation — the way back down must always be
  // cheaper than the way up (spec §3.1).
  const row = _autRow(botId, iid);
  if (!row || !row.demote) return;
  _autPost(botId, iid,
    { rung: row.demote.rung, expected_current_rung: row.rung },
    `${row.integration_label} on ${botLabel(botId)} restricted to "${row.demote.rung_label}"`);
}

function autConfirmCurrent(botId, iid) {
  // "Keep" on a not-confirmed row, or "Re-apply" on an out-of-sync row:
  // re-set the displayed level so it becomes deliberate + re-renders.
  const row = _autRow(botId, iid);
  if (!row) return;
  const body = { rung: row.rung, expected_current_rung: row.rung };
  if (row.rules && Object.keys(row.rules).length) body.rules = row.rules;
  _autPost(botId, iid, body,
    `${row.integration_label} on ${botLabel(botId)} kept at "${row.rung_label}"`);
}

function autCancelRules(botId, iid) {
  _autonomyRulesOpen.delete(_autKey(botId, iid));
  renderAutonomy();
}

function autSubmitRules(botId, iid) {
  const row = _autRow(botId, iid);
  if (!row || !row.promote) return;
  const sfx = `${botId}-${iid}`;
  const reachEl = document.getElementById(`aut-reach-${sfx}`);
  const perDayEl = document.getElementById(`aut-perday-${sfx}`);
  const reach = (reachEl && reachEl.value || '').split(',').map(s => s.trim()).filter(Boolean);
  const perDay = parseInt(perDayEl && perDayEl.value, 10);
  if (!reach.length) { toast('List at least one allowed recipient.', 'err'); return; }
  if (!perDay || perDay < 1) { toast('Set a daily limit of at least 1.', 'err'); return; }
  _autonomyRulesOpen.delete(_autKey(botId, iid));
  _autPost(botId, iid,
    { rung: row.promote.rung, rules: { reach_allow: reach, actions_per_day: perDay },
      expected_current_rung: row.rung },
    `${row.integration_label} on ${botLabel(botId)} set to "${row.promote.rung_label}"`);
}

// Inline-onclick handlers resolve via the window object; export them
// explicitly so the dependency is lint-visible instead of leaning on
// declaration-leak from script scope (the suppressions baseline only
// shrinks — new handlers must be clean).
window.autToggleExpand = autToggleExpand;
window.autPromote = autPromote;
window.autDemote = autDemote;
window.autConfirmCurrent = autConfirmCurrent;
window.autCancelRules = autCancelRules;
window.autSubmitRules = autSubmitRules;


// ── Hook Posture (Phase A of spec-hook-governance) ───────────────────────────
let _hookPostureData = null;

async function loadHookPosture(forceRescan) {
  const el = document.getElementById('hook-posture-panel');
  if (!el) return;
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  if (forceRescan) {
    try { await api('POST', '/api/hooks-admin/scan'); } catch (_e) { /* tolerated */ }
  }
  try {
    _hookPostureData = await api('GET', '/api/hooks-admin/inventory');
  } catch (e) {
    el.innerHTML = `<div class="empty">Failed to load: ${escHtml(String(e))}</div>`;
    return;
  }
  renderHookPosture();
}

function renderHookPosture() {
  const el = document.getElementById('hook-posture-panel');
  if (!el || !_hookPostureData) return;
  const bots = orderedBotIds(_hookPostureData.bots);
  if (!bots.length) {
    el.innerHTML = '<div class="empty">No bots in network.json.</div>';
    return;
  }

  // Per-bot summary tallies
  let totalIngressOn = 0, totalSilentDisable = 0, totalPolicyDrift = 0;
  let totalUnauthInj = 0, totalCmdGate = 0;
  const missingBots = [];
  for (const bid of bots) {
    const inv = _hookPostureData.bots[bid];
    if (!inv) continue;
    if (!inv.openclaw_config_present) { missingBots.push(bid); continue; }
    const resolved = (_hookPostureData.resolved || {})[bid] || {};
    const expectedPolicies = resolved.expected_plugin_policies || {};
    const trustedSet = new Set(resolved.trusted_prompt_mutators || []);
    const ingress = inv.webhook_ingress || {};
    if (ingress.configured && ingress.enabled && !resolved.webhook_ingress_enabled) totalIngressOn++;
    if (inv.self_mutation_commands_plugins || inv.self_mutation_commands_mcp) totalCmdGate++;
    for (const p of (inv.plugin_policies || [])) {
      const exp = expectedPolicies[p.plugin_name];
      if (exp) {
        if (exp.allow_conversation_access && !p.allow_conversation_access && p.plugin_enabled) totalSilentDisable++;
        else if (p.allow_conversation_access !== exp.allow_conversation_access || p.allow_prompt_injection !== exp.allow_prompt_injection) totalPolicyDrift++;
      } else if (p.allow_conversation_access || p.allow_prompt_injection) {
        totalPolicyDrift++;
      }
      if (p.allow_prompt_injection && !trustedSet.has(p.plugin_name)) totalUnauthInj++;
    }
  }

  const summary = `<div style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:14px;font-size:0.85rem">
    <div><strong>${bots.length}</strong> bot${bots.length === 1 ? '' : 's'}</div>
    <div style="color:${totalIngressOn ? 'var(--red)' : 'var(--text2)'}"><strong>${totalIngressOn}</strong> ingress unexpected-on</div>
    <div style="color:${totalSilentDisable ? 'var(--red)' : 'var(--text2)'}"><strong>${totalSilentDisable}</strong> silent-disable</div>
    <div style="color:${totalUnauthInj ? 'var(--red)' : 'var(--text2)'}"><strong>${totalUnauthInj}</strong> unauthorized prompt-injection</div>
    <div style="color:${totalPolicyDrift ? 'var(--yellow)' : 'var(--text2)'}"><strong>${totalPolicyDrift}</strong> policy drift</div>
    <div style="color:${totalCmdGate ? 'var(--yellow)' : 'var(--text2)'}"><strong>${totalCmdGate}</strong> with self-mutation</div>
    <div style="color:${missingBots.length ? 'var(--text3)' : 'var(--text2)'}"><strong>${missingBots.length}</strong> missing config${missingBots.length ? ` (${missingBots.map(escHtml).join(', ')})` : ''}</div>
  </div>`;

  // Matrix: rows = bots, columns = plugin names + ingress + cmd-gate
  // Collect every plugin seen across the pod
  const pluginSet = new Set();
  // Also include baseline-expected plugins so missing-from-bot shows
  if (_hookPostureData.baseline && _hookPostureData.baseline.pod_default) {
    for (const p of _hookPostureData.baseline.pod_default.plugin_typed_hooks || []) pluginSet.add(p.plugin_name);
  }
  for (const bid of bots) {
    const inv = _hookPostureData.bots[bid];
    if (!inv) continue;
    for (const p of (inv.plugin_policies || [])) pluginSet.add(p.plugin_name);
  }
  const cols = Array.from(pluginSet).sort();

  const headerCells = cols.map(c => `<th style="text-align:center;font-size:0.78rem">${escHtml(c)}<br><span style="font-size:0.65rem;color:var(--text3);font-weight:400">cnv/inj</span></th>`).join('');
  const rows = bots.map(bid => {
    const inv = _hookPostureData.bots[bid];
    const resolved = (_hookPostureData.resolved || {})[bid] || {};
    const expectedPolicies = resolved.expected_plugin_policies || {};
    const trustedSet = new Set(resolved.trusted_prompt_mutators || []);

    if (!inv) {
      return `<tr><td><strong>${escHtml(botLabel(bid))}</strong></td><td colspan="${cols.length + 1}" style="text-align:center;color:var(--text3)">no inventory</td></tr>`;
    }
    if (!inv.openclaw_config_present) {
      return `<tr><td><strong>${escHtml(botLabel(bid))}</strong></td><td colspan="${cols.length + 1}" style="text-align:center;color:var(--text3)">no openclaw.json</td></tr>`;
    }
    const policiesByName = {};
    for (const p of (inv.plugin_policies || [])) policiesByName[p.plugin_name] = p;
    const ingress = inv.webhook_ingress || {};
    const ingressBadge = (ingress.configured && ingress.enabled && !resolved.webhook_ingress_enabled)
      ? '<span class="badge badge-red" title="webhook ingress enabled, baseline expects off">ingress!</span>'
      : (ingress.configured && ingress.enabled)
      ? '<span class="badge badge-yellow">ingress on</span>'
      : '<span style="color:var(--text3)">·</span>';

    const cells = cols.map(c => {
      const p = policiesByName[c];
      const exp = expectedPolicies[c];
      if (!p) {
        if (exp) return '<td style="text-align:center"><span class="badge badge-yellow" title="baseline expects this plugin">exp?</span></td>';
        return '<td style="text-align:center;color:var(--text3)">·</td>';
      }
      let convCell, injCell;
      if (p.allow_conversation_access) {
        convCell = '<span class="badge badge-green" title="enabled">on</span>';
      } else if (exp && exp.allow_conversation_access && p.plugin_enabled) {
        convCell = '<span class="badge badge-red" title="silent-disable">off!</span>';
      } else {
        convCell = '<span style="color:var(--text3)">off</span>';
      }
      if (p.allow_prompt_injection) {
        injCell = trustedSet.has(c)
          ? '<span class="badge badge-yellow" title="approved exception">on</span>'
          : '<span class="badge badge-red" title="not in trusted_prompt_mutators">on!</span>';
      } else {
        injCell = '<span style="color:var(--text3)">off</span>';
      }
      return `<td style="text-align:center;font-size:0.75rem">${convCell}<br>${injCell}</td>`;
    }).join('');

    const cmdGate = (inv.self_mutation_commands_plugins || inv.self_mutation_commands_mcp)
      ? '<span class="badge badge-yellow" style="margin-left:4px" title="commands.plugins or commands.mcp = true">self-mut</span>'
      : '';

    return `<tr><td><strong>${escHtml(botLabel(bid))}</strong>${cmdGate} ${ingressBadge}</td>${cells}</tr>`;
  }).join('');

  el.innerHTML = `${summary}
    <div style="overflow-x:auto">
      <table class="data-table" style="min-width:100%">
        <thead><tr><th>Bot</th>${headerCells}</tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div style="margin-top:10px;font-size:0.72rem;color:var(--text3)">
      Each plugin cell shows two flags: <em>conv</em> (allowConversationAccess) on top, <em>inj</em> (allowPromptInjection) below.
      <span class="badge badge-green">on</span> active per baseline ·
      <span class="badge badge-red">off!</span> silent-disable ·
      <span class="badge badge-red">on!</span> allowPromptInjection outside trusted-mutators ·
      <span class="badge badge-yellow">on</span> trusted exception ·
      <span class="badge badge-yellow">exp?</span> baseline expects this plugin but bot doesn't have it ·
      <span style="color:var(--text3)">·</span> no plugin entry.
    </div>`;
}

// ── Content Scan (Phase A of spec-prompt-injection-scanner) ──────────────────
let _contentScanData = null;
let _contentScanCatalog = null;
let _contentScanSuppressions = null;
let _contentScanSubtab = 'summary';

function csTab(tab) {
  _contentScanSubtab = tab;
  ['summary', 'catalog', 'suppressions'].forEach(t => {
    document.getElementById('content-scan-' + t + '-panel').style.display = (t === tab) ? '' : 'none';
    document.getElementById('cs-tab-' + t).classList.toggle('active', t === tab);
  });
  if (tab === 'catalog' && !_contentScanCatalog) loadContentScanCatalog();
  if (tab === 'suppressions' && !_contentScanSuppressions) loadContentScanSuppressions();
}

function _csSeverityBadge(sev) {
  if (sev === 'alert') return '<span class="badge badge-red">alert</span>';
  if (sev === 'warn')  return '<span class="badge badge-yellow">warn</span>';
  if (sev === 'info')  return '<span class="badge badge-blue">info</span>';
  return '<span class="badge badge-green">clear</span>';
}

function _csEscape(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function loadContentScan(forceRescan) {
  const el = document.getElementById('content-scan-summary-panel');
  if (!el) return;
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  if (forceRescan) {
    try { await api('POST', '/api/content-scan/scan'); } catch (_e) { /* tolerated */ }
  }
  let data;
  try {
    data = await api('GET', '/api/content-scan/inventory');
  } catch (e) {
    el.innerHTML = '<div class="error">Inventory load failed: ' + _csEscape(e && e.message || e) + '</div>';
    return;
  }
  _contentScanData = data;
  renderContentScanSummary();
}

function renderContentScanSummary() {
  const el = document.getElementById('content-scan-summary-panel');
  if (!_contentScanData) { el.innerHTML = '<div class="loading">Loading…</div>'; return; }
  const bots = _contentScanData.bots || [];
  const pod = _contentScanData.pod_files || [];

  if (!bots.length && !pod.length) {
    el.innerHTML = '<div style="font-size:0.82rem;color:var(--text2);padding:18px">No scan results yet — click <strong>Re-scan</strong> above to run the content scanner once.</div>';
    return;
  }

  const cardForBot = (b) => {
    const sev = b.highest_severity || 'clear';
    const matches = b.files_with_matches || 0;
    return `
      <div class="card" style="padding:12px;cursor:pointer" onclick="csShowBot('${_csEscape(b.bot_id)}')">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div style="font-weight:600">${_csEscape(botLabel(b.bot_id))}</div>
          ${_csSeverityBadge(sev)}
        </div>
        <div style="font-size:0.75rem;color:var(--text2);margin-top:6px">
          ${b.files_scanned} file(s) scanned · ${matches} with match${matches === 1 ? '' : 'es'}
        </div>
        <div style="font-size:0.7rem;color:var(--text3);margin-top:4px">
          Last: ${_csEscape(b.last_scanned_at || '—')}
        </div>
      </div>
    `;
  };

  const podSection = (() => {
    if (!pod.length) return '';
    const totalMatches = pod.reduce((s, f) => s + (f.matches || 0), 0);
    return `
      <div class="section-head" style="margin-top:18px">Pod-wide files</div>
      <table class="table">
        <thead><tr><th>File</th><th>Matches</th><th>Severity</th><th>Last scanned</th><th>Action</th></tr></thead>
        <tbody>
          ${pod.map(f => `
            <tr>
              <td><code>${_csEscape(f.file)}</code></td>
              <td>${f.matches || 0}</td>
              <td>${_csSeverityBadge(f.highest_severity || 'clear')}</td>
              <td style="font-size:0.72rem;color:var(--text3)">${_csEscape(f.scanned_at || '')}</td>
              <td><button class="btn btn-ghost btn-sm" onclick="csShowFile('__pod__','${_csEscape(f.file)}')">Detail →</button></td>
            </tr>
          `).join('')}
        </tbody>
      </table>
      <div style="font-size:0.72rem;color:var(--text3);margin-top:4px">${totalMatches} match(es) across pod-wide files.</div>
    `;
  })();

  document.getElementById('content-scan-summary-panel').innerHTML = `
    <div class="section-head" style="margin-top:0">Per-bot summary</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px">
      ${bots.map(cardForBot).join('')}
    </div>
    ${podSection}
    <div id="content-scan-detail-pane" style="margin-top:18px"></div>
  `;
}

function csShowBot(botId) {
  if (!_contentScanData) return;
  const bots = (_contentScanData.bots || []);
  const bot = bots.find(b => b.bot_id === botId);
  const detail = document.getElementById('content-scan-detail-pane');
  if (!bot || !detail) return;
  detail.innerHTML = `
    <div class="section-head">${_csEscape(botId)} files</div>
    <table class="table">
      <thead><tr><th>File</th><th>Matches</th><th>Severity</th><th>Hash</th><th>Last scanned</th><th>Action</th></tr></thead>
      <tbody>
        ${(bot.files || []).map(f => `
          <tr>
            <td><code>${_csEscape(f.file)}</code></td>
            <td>${f.matches || 0}</td>
            <td>${_csSeverityBadge(f.highest_severity || 'clear')}</td>
            <td style="font-family:monospace;font-size:0.7rem;color:var(--text3)">${_csEscape((f.file_hash || '').slice(0, 12))}</td>
            <td style="font-size:0.72rem;color:var(--text3)">${_csEscape(f.scanned_at || '')}</td>
            <td><button class="btn btn-ghost btn-sm" onclick="csShowFile('${_csEscape(botId)}','${_csEscape(f.file)}')">Detail →</button></td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
  detail.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function csShowFile(botId, file) {
  const detail = document.getElementById('content-scan-detail-pane');
  if (!detail) return;
  detail.innerHTML = '<div class="loading"><div class="spinner"></div> Loading file detail…</div>';
  let res;
  try {
    res = await api('GET', '/api/content-scan/file?bot_id=' + encodeURIComponent(botId) + '&file=' + encodeURIComponent(file));
  } catch (e) {
    detail.innerHTML = '<div class="error">Failed to load file detail: ' + _csEscape(e && e.message || e) + '</div>';
    return;
  }
  const r = res.result || {};
  const matches = r.matches || [];
  const matchRows = matches.map((m, idx) => `
    <tr>
      <td>${_csSeverityBadge(m.severity || 'warn')}</td>
      <td><code>${_csEscape(m.pattern_id)}</code></td>
      <td style="font-family:monospace;font-size:0.72rem">L${m.line}:C${m.column_start}</td>
      <td style="font-family:monospace;font-size:0.72rem;white-space:pre-wrap;max-width:540px;overflow-wrap:anywhere">${_csEscape(m.excerpt || '')}</td>
      <td>
        <button class="btn btn-ghost btn-sm" onclick="csMarkReviewed('${_csEscape(botId)}','${_csEscape(file)}',${idx})">Mark Reviewed</button>
      </td>
    </tr>
  `).join('');

  detail.innerHTML = `
    <div class="section-head">
      ${_csEscape(botId)} · <code>${_csEscape(file)}</code>
      <div style="display:flex;gap:8px">
        <button class="btn btn-ghost btn-sm" onclick="csViewRaw('${_csEscape(botId)}','${_csEscape(file)}')">View raw file</button>
      </div>
    </div>
    <div style="font-size:0.72rem;color:var(--text3);margin-bottom:8px">
      ${_csEscape(r.absolute_path || '')} · ${(r.file_size_bytes || 0)} bytes · hash <code>${_csEscape((r.file_hash || '').slice(0, 16))}</code> · scanned ${_csEscape(r.scanned_at || '')}
    </div>
    ${matches.length === 0
      ? '<div class="badge badge-green">All clear — no patterns matched.</div>'
      : `<table class="table">
          <thead><tr><th>Severity</th><th>Pattern</th><th>Location</th><th>Excerpt</th><th>Action</th></tr></thead>
          <tbody>${matchRows}</tbody>
        </table>`
    }
    <div id="cs-raw-pane" style="margin-top:14px"></div>
  `;
  detail.scrollIntoView({ behavior: 'smooth', block: 'start' });
  // Cache the matches keyed on the detail element so Mark Reviewed can look them up.
  detail._matches = matches;
  detail._file = file;
  detail._botId = botId;
  // Mirror csShowBot's scroll behavior — without this, clicking Detail swaps
  // the pane in place and looks like nothing happened if the table was
  // already off-screen.
  detail.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function csViewRaw(botId, file) {
  const pane = document.getElementById('cs-raw-pane');
  if (!pane) return;
  pane.innerHTML = '<div class="loading">Loading raw file…</div>';
  let res;
  try {
    res = await api('POST', '/api/content-scan/file-content', { bot_id: botId, file: file });
  } catch (e) {
    pane.innerHTML = '<div class="error">Read failed: ' + _csEscape(e && e.message || e) + '</div>';
    return;
  }
  pane.innerHTML = `
    <div class="section-head">Raw file content${res.truncated ? ' (truncated to 200KB)' : ''}</div>
    <pre style="background:var(--bg2);padding:10px;font-size:0.72rem;max-height:400px;overflow:auto;white-space:pre-wrap">${_csEscape(res.content || '')}</pre>
  `;
}

async function csMarkReviewed(botId, file, idx) {
  const detail = document.getElementById('content-scan-detail-pane');
  if (!detail || !detail._matches) return;
  const m = detail._matches[idx];
  if (!m) return;
  const note = prompt('Optional reviewer note for this suppression:', '') || '';
  try {
    await api('POST', '/api/content-scan/mark-reviewed', {
      bot_id: botId,
      file: file,
      pattern_id: m.pattern_id,
      line_range: [m.line, m.line],
      excerpt: m.excerpt || '',
      reviewer_note: note,
    });
  } catch (e) {
    toast('Mark Reviewed failed: ' + (e && e.message || e), 'err');
    return;
  }
  // Refresh the file detail to drop the suppressed row (it'll come back next scan
  // if not suppressed, but the cached result still shows it — so just rerun the
  // scan and reload the detail).
  try { await api('POST', '/api/content-scan/scan'); } catch (_e) { /* tolerated */ }
  csShowFile(botId, file);
  _contentScanSuppressions = null;
}

async function loadContentScanCatalog() {
  const el = document.getElementById('content-scan-catalog-panel');
  if (!el) return;
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  let res;
  try {
    res = await api('GET', '/api/content-scan/catalog');
  } catch (e) {
    el.innerHTML = '<div class="error">Catalog load failed: ' + _csEscape(e && e.message || e) + '</div>';
    return;
  }
  _contentScanCatalog = res.catalog;
  const cat = res.catalog || {};
  const patterns = cat.deny_patterns || [];
  const allowlist = cat.evolve_markers_allowlist || [];
  el.innerHTML = `
    <div style="font-size:0.75rem;color:var(--text3);margin-bottom:8px">
      Catalog version ${cat.version || 1}. Edits flow through <code>UpdateContentScanCatalog</code>
      proposals — the operator approves them on the Maintenance · Alerts tab before the
      catalog file is rewritten.
    </div>
    <div class="section-head" style="margin-top:0">Deny patterns (${patterns.length})</div>
    <table class="table">
      <thead><tr><th>ID</th><th>Kind</th><th>Severity</th><th>Description</th><th>Detail</th><th></th></tr></thead>
      <tbody>
        ${patterns.map(p => `
          <tr>
            <td><code>${_csEscape(p.id)}</code></td>
            <td>${_csEscape(p.kind)}</td>
            <td>${_csSeverityBadge(p.severity || 'warn')}</td>
            <td style="font-size:0.78rem">${_csEscape(p.description || '')}</td>
            <td style="font-family:monospace;font-size:0.7rem;color:var(--text3);max-width:300px;overflow-wrap:anywhere">${_csEscape(
              p.kind === 'regex' ? p.pattern :
              p.kind === 'line_length' ? ('threshold=' + p.threshold) :
              p.kind === 'structural' ? ('min_size_bytes=' + p.min_size_bytes + ' applies_to=' + JSON.stringify(p.applies_to || [])) :
              ''
            )}</td>
            <td style="text-align:right;white-space:nowrap">
              <button class="btn btn-ghost btn-sm"
                onclick="csProposeRemovePattern('${_csEscape(p.id).replace(/'/g, "\\'")}')"
                title="Remove this pattern from the catalog">
                Remove…
              </button>
            </td>
          </tr>
        `).join('')}
      </tbody>
    </table>
    <div class="section-head">Evolve-marker allowlist (${allowlist.length})</div>
    <ul style="font-family:monospace;font-size:0.78rem;padding-left:20px">
      ${allowlist.map(m => `<li>${_csEscape(m)}</li>`).join('')}
    </ul>
  `;
}

async function csProposeRemovePattern(patternId) {
  if (!patternId) return;
  if (!await confirmModal({ body: `Remove pattern \`${patternId}\` from the content-scan catalog?`, danger: true })) return;
  try {
    const resp = await api('POST', '/api/content-scan/propose-catalog-update', {
      operation: 'remove_pattern',
      fields: { pattern_id: patternId },
    });
    if (handleOperatorProposalResult(`Pattern '${patternId}' removed`, resp)) {
      if (typeof loadContentScanCatalog === 'function') loadContentScanCatalog();
    }
  } catch (e) {
    toast(`Propose failed: ${(e && e.message) || e}`, 'err');
  }
}

async function loadContentScanSuppressions() {
  const el = document.getElementById('content-scan-suppressions-panel');
  if (!el) return;
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  let res;
  try {
    res = await api('GET', '/api/content-scan/suppressions');
  } catch (e) {
    el.innerHTML = '<div class="error">Load failed: ' + _csEscape(e && e.message || e) + '</div>';
    return;
  }
  _contentScanSuppressions = res.suppressions || [];
  if (!_contentScanSuppressions.length) {
    el.innerHTML = '<div style="font-size:0.82rem;color:var(--text2);padding:18px">No active suppressions. Mark a content-scan match as Reviewed from a file detail page to add one.</div>';
    return;
  }
  el.innerHTML = `
    <div style="font-size:0.75rem;color:var(--text3);margin-bottom:10px">
      Active suppressions silence matching content-scan matches for 30 days
      (or ~10 years if graduated). After expiry the match re-fires unless the
      operator re-confirms. <strong>Graduate</strong> extends the expiry to
      ~10 years for matches the operator has decided are permanent — useful for
      false-positives the catalog can't yet describe.
    </div>
    <table class="table">
      <thead><tr><th>Bot</th><th>File</th><th>Pattern</th><th>Lines</th><th>Reviewed</th><th>Expires</th><th>Note</th><th>Action</th></tr></thead>
      <tbody>
        ${_contentScanSuppressions.map(s => `
          <tr>
            <td><code>${_csEscape(s.bot_id)}</code></td>
            <td><code>${_csEscape(s.file)}</code></td>
            <td><code>${_csEscape(s.pattern_id)}</code></td>
            <td style="font-family:monospace;font-size:0.72rem">${_csEscape((s.line_range || []).join('–'))}</td>
            <td style="font-size:0.72rem;color:var(--text3)">${_csEscape(s.reviewed_at)}</td>
            <td style="font-size:0.72rem;color:var(--text3)">${_csExpiresCell(s.expires_at)}</td>
            <td style="font-size:0.75rem">${_csEscape(s.reviewer_note || '')}</td>
            <td style="white-space:nowrap">
              ${_csIsGraduated(s.expires_at) ? '' : `<button class="btn btn-ghost btn-sm" onclick='csGraduateSuppression(${JSON.stringify(s).replace(/'/g, "&#39;")})' title="Extend this suppression's TTL to ~10 years">Graduate</button>`}
              <button class="btn btn-ghost btn-sm" onclick='csRemoveSuppression(${JSON.stringify(s).replace(/'/g, "&#39;")})'>Remove</button>
            </td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

// A graduated suppression has an expiry many years out (the API uses
// PERMANENT_TTL_DAYS=3650 = ~10y). UI threshold: if the suppression
// expires more than 2 years from now, treat it as graduated.
function _csIsGraduated(expiresAt) {
  if (!expiresAt) return false;
  const exp = Date.parse(expiresAt);
  if (Number.isNaN(exp)) return false;
  return exp > Date.now() + 2 * 365 * 24 * 3600 * 1000;
}

function _csExpiresCell(expiresAt) {
  if (!expiresAt) return '';
  if (_csIsGraduated(expiresAt)) {
    return `<span class="badge" style="background:rgba(180,127,255,0.15);color:#b47fff;font-size:0.7rem;padding:1px 6px" title="${_csEscape(expiresAt)}">graduated</span>`;
  }
  return _csEscape(expiresAt);
}

async function csGraduateSuppression(s) {
  if (!await confirmModal(
    `Graduate this suppression to ~10 years?\n\n` +
    `(${s.bot_id}/${s.file} pattern=${s.pattern_id} lines=${(s.line_range || []).join('-')})\n\n` +
    `Use when this match is a known false-positive that won't change. ` +
    `The default 30-day expiry forces re-review every month; graduated suppressions ` +
    `silence the same match for ~10 years.`
  )) return;
  try {
    await api('POST', '/api/content-scan/graduate', {
      bot_id: s.bot_id,
      file: s.file,
      pattern_id: s.pattern_id,
      line_range: s.line_range || [],
    });
  } catch (e) {
    toast('Graduate failed: ' + (e && e.message || e), 'err');
    return;
  }
  _contentScanSuppressions = null;
  loadContentScanSuppressions();
}

async function csRemoveSuppression(s) {
  if (!await confirmModal({ body: 'Remove this suppression? The next scan will re-emit the signal if the match still applies.', danger: true })) return;
  try {
    await api('POST', '/api/content-scan/unsuppress', {
      bot_id: s.bot_id, file: s.file, pattern_id: s.pattern_id,
      line_range: s.line_range || [],
    });
  } catch (e) {
    toast('Remove failed: ' + (e && e.message || e), 'err');
    return;
  }
  _contentScanSuppressions = null;
  loadContentScanSuppressions();
}

// ── Plugin Posture (Phase A of spec-plugin-inventory) ────────────────────────
let _pluginPostureData = null;

async function loadPluginPosture(forceRescan) {
  const el = document.getElementById('plugin-posture-panel');
  if (!el) return;
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  if (forceRescan) {
    try { await api('POST', '/api/plugins-admin/scan'); } catch (_e) { /* tolerated */ }
  }
  try {
    _pluginPostureData = await api('GET', '/api/plugins-admin/inventory');
  } catch (e) {
    el.innerHTML = `<div class="empty">Failed to load: ${escHtml(String(e))}</div>`;
    return;
  }
  renderPluginPosture();
}

function renderPluginPosture() {
  const el = document.getElementById('plugin-posture-panel');
  if (!el || !_pluginPostureData) return;
  const bots = orderedBotIds(_pluginPostureData.bots);
  if (!bots.length) {
    el.innerHTML = '<div class="empty">No bots in network.json.</div>';
    return;
  }

  // Build the column set: union of all plugin names observed across the pod
  // plus any required/denied names from the baseline (so they're visible
  // even when no bot has them as an entry). The v2 baseline has these
  // fields at the top level (no more pod_default wrapper).
  const colSet = new Set();
  const baseline = _pluginPostureData.baseline || {};
  (baseline.required_plugins || []).forEach(n => colSet.add(n));
  (baseline.denied_plugins  || []).forEach(n => colSet.add(n));
  for (const bid of bots) {
    const inv = _pluginPostureData.bots[bid];
    if (!inv) continue;
    (inv.entries || []).forEach(e => colSet.add(e.name));
  }
  const cols = Array.from(colSet).sort();

  // Per-bot summary. v2 only tracks four monitor conditions:
  // missing_required, denied_present, load_path_unexpected, command_gate.
  // The five retired drift counters (unexpected_enabled / unexpected_disabled
  // / allow_list_missing / allow_list_drift / source_unauthorized) come out.
  let missingReq = 0, deniedPresent = 0, loadPathDrift = 0, cmdGate = 0;
  const missingBots = [];
  for (const bid of bots) {
    const inv = _pluginPostureData.bots[bid];
    if (!inv) continue;
    if (!inv.openclaw_config_present) { missingBots.push(bid); continue; }
    const resolved = (_pluginPostureData.resolved || {})[bid] || {};
    const required = new Set(resolved.required || []);
    const denied = new Set(resolved.denied || []);
    const expectedLoadPaths = new Set(resolved.expected_load_paths || []);
    const enabledNames = new Set((inv.entries || []).filter(e => e.enabled).map(e => e.name));

    for (const r of required) if (!enabledNames.has(r)) missingReq++;
    for (const d of denied) if (enabledNames.has(d)) deniedPresent++;
    for (const p of (inv.load_paths || [])) if (!expectedLoadPaths.has(p)) loadPathDrift++;
    if (inv.self_mutation_commands_plugins) cmdGate++;
  }

  const summary = `
    <div style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:14px;font-size:0.85rem">
      <div><strong>${bots.length}</strong> bot${bots.length === 1 ? '' : 's'}</div>
      <div><strong>${cols.length}</strong> plugin${cols.length === 1 ? '' : 's'} observed</div>
      <div style="color:${missingReq ? 'var(--red)' : 'var(--text2)'}"><strong>${missingReq}</strong> missing required</div>
      <div style="color:${deniedPresent ? 'var(--red)' : 'var(--text2)'}"><strong>${deniedPresent}</strong> denied present</div>
      <div style="color:${loadPathDrift ? 'var(--red)' : 'var(--text2)'}"><strong>${loadPathDrift}</strong> load-path drift</div>
      <div style="color:${cmdGate ? 'var(--yellow)' : 'var(--text2)'}"><strong>${cmdGate}</strong> with self-mutation</div>
      <div style="color:${missingBots.length ? 'var(--text3)' : 'var(--text2)'}"><strong>${missingBots.length}</strong> missing config${missingBots.length ? ` (${missingBots.map(escHtml).join(', ')})` : ''}</div>
    </div>`;

  // Matrix: rows = bots, cols = plugin names. v2 cell semantics:
  //   green "on" : enabled (with provenance in the tooltip when available)
  //   red "denied!" : enabled but in denied set
  //   red "req!" : required but no entry exists
  //   red "req-off!" : required but the entry is disabled
  //   muted ○ : entry exists but is disabled (no baseline reason to flag)
  //   "·" : no entry on this bot
  //   "—" : no openclaw.json
  // Retired (v1): "unex" / "exp?" / "exp-off" — depended on expected_enabled
  // and permitted_enabled, which the v2 baseline doesn't carry.
  const headerCells = cols.map(c => `<th style="text-align:center;font-size:0.78rem">${escHtml(c)}</th>`).join('');
  const provLabel = (e) => {
    const bits = [];
    if (e.resolved_name) {
      bits.push(e.resolved_name + (e.resolved_version ? '@' + e.resolved_version : ''));
    } else if (e.install_spec) {
      bits.push(e.install_spec);
    } else if (e.install_source) {
      bits.push(e.install_source);
    }
    if (e.clawhub_channel) bits.push('clawhub:' + e.clawhub_channel);
    return bits.join(' · ');
  };
  const rows = bots.map(bid => {
    const inv = _pluginPostureData.bots[bid];
    const resolved = (_pluginPostureData.resolved || {})[bid] || {};
    const required = new Set(resolved.required || []);
    const denied = new Set(resolved.denied || []);
    let cellEntries;
    if (!inv) {
      cellEntries = new Map();
    } else if (!inv.openclaw_config_present) {
      cellEntries = null;
    } else {
      cellEntries = new Map();
      (inv.entries || []).forEach(e => cellEntries.set(e.name, e));
    }
    const cells = cols.map(c => {
      if (cellEntries === null) return '<td style="text-align:center;color:var(--text3)">—</td>';
      const e = cellEntries.get(c);
      const isReq = required.has(c);
      const isDeny = denied.has(c);
      if (!e) {
        if (isReq) return '<td style="text-align:center"><span class="badge badge-red" title="required, missing">req!</span></td>';
        return '<td style="text-align:center;color:var(--text3)">·</td>';
      }
      if (e.enabled) {
        if (isDeny) return '<td style="text-align:center"><span class="badge badge-red" title="denied but enabled">denied!</span></td>';
        const prov = provLabel(e);
        const tip = prov ? ` title="${escHtml(prov)}"` : '';
        return `<td style="text-align:center"><span class="badge badge-green"${tip}>on</span></td>`;
      }
      if (isReq) return '<td style="text-align:center"><span class="badge badge-red" title="required but disabled">req-off!</span></td>';
      return '<td style="text-align:center;color:var(--text3)" title="disabled">○</td>';
    }).join('');
    const badges = [];
    if (inv && inv.self_mutation_commands_plugins) badges.push('<span class="badge badge-yellow" style="margin-left:4px" title="commands.plugins = true">self-mut</span>');
    return `<tr><td><strong>${escHtml(botLabel(bid))}</strong>${badges.join('')}</td>${cells}</tr>`;
  }).join('');

  el.innerHTML = `${summary}
    <div style="overflow-x:auto">
      <table class="data-table" style="min-width:100%">
        <thead><tr><th>Bot</th>${headerCells}</tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div style="margin-top:10px;font-size:0.72rem;color:var(--text3)">
      <span class="badge badge-green">on</span> enabled (hover for provenance) ·
      <span class="badge badge-red">req!</span> required but absent ·
      <span class="badge badge-red">denied!</span> denied but enabled ·
      <span style="color:var(--text3)">○</span> disabled ·
      <span style="color:var(--text3)">·</span> no entry ·
      <span style="color:var(--text3)">—</span> no openclaw.json.
    </div>`;
}

// ── MCP Posture (Phase A of spec-mcp-administration) ─────────────────────────
let _mcpPostureData = null;

async function loadMcpPosture(forceRescan) {
  const el = document.getElementById('mcp-posture-panel');
  if (!el) return;
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  if (forceRescan) {
    try { await api('POST', '/api/mcp-admin/scan'); } catch (_e) { /* tolerated */ }
  }
  try {
    _mcpPostureData = await api('GET', '/api/mcp-admin/inventory');
  } catch (e) {
    el.innerHTML = `<div class="empty">Failed to load: ${escHtml(String(e))}</div>`;
    return;
  }
  renderMcpPosture();
}

function renderMcpPosture() {
  const el = document.getElementById('mcp-posture-panel');
  if (!el || !_mcpPostureData) return;
  const bots = orderedBotIds(_mcpPostureData.bots);
  if (!bots.length) {
    el.innerHTML = '<div class="empty">No bots in network.json.</div>';
    return;
  }
  const allowEntries = (_mcpPostureData.allowlist && _mcpPostureData.allowlist.entries) || [];
  const findAllow = (botId, name) => allowEntries.find(e => e.name === name && (e.bot_id === botId || e.bot_id === '*'));

  // Collect every distinct server name observed across the pod (plus any
  // allowlist names not yet observed, so the matrix shows expected-but-
  // -missing as a future-Phase-B "missing required" cell).
  const serverNames = new Set();
  for (const bid of bots) {
    const inv = _mcpPostureData.bots[bid];
    if (inv && inv.servers) inv.servers.forEach(s => serverNames.add(s.name));
  }
  const cols = Array.from(serverNames).sort();

  // Per-bot summary stats
  let totalUnknown = 0, totalDrift = 0, totalServers = 0, missingConfigBots = [], selfMutationBots = [];
  let totalUnhealthy = 0, totalCredInvalid = 0, totalCveMatches = 0;
  for (const bid of bots) {
    const inv = _mcpPostureData.bots[bid];
    if (!inv) continue;
    if (!inv.openclaw_config_present) { missingConfigBots.push(bid); continue; }
    if (inv.self_mutation_commands_mcp || inv.self_mutation_commands_plugins) selfMutationBots.push(bid);
    for (const s of inv.servers || []) {
      totalServers++;
      const allow = findAllow(bid, s.name);
      if (!allow) totalUnknown++;
      else if (allow.config_signature && allow.config_signature !== s.config_signature) totalDrift++;
      const lp = s.last_probe;
      if (lp && !lp.ok) {
        if (lp.error_class === 'credential_invalid') totalCredInvalid++;
        else totalUnhealthy++;
      }
      if (s.advisory_count && s.advisory_count > 0) totalCveMatches += s.advisory_count;
    }
  }

  const summary = `
    <div style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:14px;font-size:0.85rem">
      <div><strong>${bots.length}</strong> bot${bots.length === 1 ? '' : 's'}</div>
      <div><strong>${totalServers}</strong> server${totalServers === 1 ? '' : 's'} observed</div>
      <div style="color:${totalUnknown ? 'var(--red)' : 'var(--text2)'}"><strong>${totalUnknown}</strong> unknown</div>
      <div style="color:${totalDrift ? 'var(--yellow)' : 'var(--text2)'}"><strong>${totalDrift}</strong> drifted</div>
      <div style="color:${totalCredInvalid ? 'var(--red)' : 'var(--text2)'}"><strong>${totalCredInvalid}</strong> cred-invalid</div>
      <div style="color:${totalUnhealthy ? 'var(--yellow)' : 'var(--text2)'}"><strong>${totalUnhealthy}</strong> unhealthy</div>
      <div style="color:${totalCveMatches ? 'var(--red)' : 'var(--text2)'}"><strong>${totalCveMatches}</strong> CVE match${totalCveMatches === 1 ? '' : 'es'}</div>
      <div style="color:${missingConfigBots.length ? 'var(--text3)' : 'var(--text2)'}"><strong>${missingConfigBots.length}</strong> bot${missingConfigBots.length === 1 ? '' : 's'} missing config${missingConfigBots.length ? ` (${missingConfigBots.map(escHtml).join(', ')})` : ''}</div>
      <div style="color:${selfMutationBots.length ? 'var(--yellow)' : 'var(--text2)'}"><strong>${selfMutationBots.length}</strong> with self-mutation${selfMutationBots.length ? ` (${selfMutationBots.map(escHtml).join(', ')})` : ''}</div>
    </div>`;

  if (cols.length === 0) {
    el.innerHTML = `${summary}
      <div class="card" style="padding:14px;color:var(--text2);font-size:0.85rem">
        ✓ No MCP servers configured on any bot. Clean baseline.
      </div>`;
    return;
  }

  // Build matrix
  const headerCells = cols.map(c => `<th style="text-align:center">${escHtml(c)}</th>`).join('');
  const rows = bots.map(bid => {
    const inv = _mcpPostureData.bots[bid];
    const botCells = cols.map(c => {
      if (!inv) return '<td style="text-align:center;color:var(--text3)">—</td>';
      if (!inv.openclaw_config_present) return '<td style="text-align:center;color:var(--text3)">—</td>';
      const server = (inv.servers || []).find(s => s.name === c);
      if (!server) return '<td style="text-align:center;color:var(--text3)">·</td>';
      const allow = findAllow(bid, c);
      if (!allow) return '<td style="text-align:center"><span class="badge badge-red" title="not in allowlist">unknown</span></td>';
      if (allow.config_signature && allow.config_signature !== server.config_signature) {
        return '<td style="text-align:center"><span class="badge badge-yellow" title="config drift">drift</span></td>';
      }
      return '<td style="text-align:center"><span class="badge badge-green">ok</span></td>';
    }).join('');
    const sm = inv && (inv.self_mutation_commands_mcp || inv.self_mutation_commands_plugins);
    // Renamed from "botLabel" to avoid shadowing the global helper.
    const botCellHtml = sm
      ? `<strong>${escHtml(botLabel(bid))}</strong> <span class="badge badge-yellow" style="margin-left:4px" title="commands.mcp or commands.plugins = true">self-mutation</span>`
      : `<strong>${escHtml(botLabel(bid))}</strong>`;
    return `<tr><td>${botCellHtml}</td>${botCells}</tr>`;
  }).join('');

  el.innerHTML = `${summary}
    <table class="data-table" style="width:100%">
      <thead><tr><th>Bot</th>${headerCells}</tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div style="margin-top:10px;font-size:0.72rem;color:var(--text3)">
      Cells: <span class="badge badge-green">ok</span> approved + signature match ·
      <span class="badge badge-yellow">drift</span> allowlist entry exists with different signature ·
      <span class="badge badge-red">unknown</span> server not in allowlist ·
      <span style="color:var(--text3)">·</span> not configured on this bot ·
      <span style="color:var(--text3)">—</span> bot has no openclaw.json.
    </div>`;
}

async function loadConfigHealth() {
  const el = document.getElementById('config-health-panel');
  if (!el) return;
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Checking config…</div>';
  const d = await api('GET', '/api/config/health');
  if (!d || d.error) {
    el.innerHTML = `<div class="alert alert-error">${escHtml(d?.error || 'Failed to load config health')}</div>`;
    return;
  }
  _healthData = d;
  renderConfigHealth();
}

function _healthToggleRow(cid) {
  if (_healthExpanded.has(cid)) _healthExpanded.delete(cid);
  else _healthExpanded.add(cid);
  renderConfigHealth();
}

function _healthAllSummary(botEntries) {
  // Backend emits these four check ids in _check_bot (server.py). The
  // frontend previously also expected an 'auth_order' row, but no
  // corresponding backend check was ever implemented — it would render
  // as an em-dash for every bot. The auth-provider precedence list is
  // already covered as a fallback inside the primary_model check, so
  // the standalone row was removed.
  const CHECK_IDS = ['gateway_auth','compaction','evolve_plugin','primary_model'];
  const CHECK_LABELS = {
    gateway_auth: 'Gateway auth',
    compaction: 'Compaction',
    evolve_plugin: 'Evolve plugin',
    primary_model: 'Primary model',
  };

  // Count issues — low-severity items are informational and don't count
  let issues = [];
  for (const [bot, d] of botEntries) {
    for (const c of (d.checks || [])) {
      if (c.status !== 'ok' && c.severity !== 'low') issues.push(`${escHtml(botLabel(bot))}: ${escHtml(c.label)} — ${escHtml(c.detail)}`);
    }
  }

  const botNames = botEntries.map(([b]) => b);
  const headerCols = botNames.map(b => `<th style="text-align:center;padding:6px 14px;font-size:0.72rem;color:var(--text3);text-transform:uppercase;letter-spacing:.04em">${escHtml(botLabel(b))}</th>`).join('');
  const colCount = botNames.length + 1;

  const rows = CHECK_IDS.map(cid => {
    const label = CHECK_LABELS[cid] || cid;
    const isOpen = _healthExpanded.has(cid);
    // Phase 9 — chevron is now an .expand-icon SVG span; is-open
    // rotates it. See style-guide §9.13.
    const chev = `<span class="expand-icon${isOpen ? ' is-open' : ''}" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>`;

    const cols = botNames.map(bot => {
      const d = botEntries.find(([b]) => b === bot)?.[1];
      const c = (d?.checks || []).find(x => x.id === cid);
      const icon = c ? c.icon : '—';
      return `<td style="text-align:center;font-size:1rem">${icon}</td>`;
    }).join('');

    // Per-bot detail rows surfaced in the expansion. Carries everything
    // the old per-bot tiles carried (severity, detail string, fix link).
    const detailItems = botEntries.map(([bot, d]) => {
      if (d.error) {
        return `<div style="display:flex;align-items:flex-start;gap:10px;padding:6px 0;border-bottom:1px solid var(--border)">
          <span style="flex:0 0 110px;font-weight:500;font-size:0.82rem">${escHtml(botLabel(bot))}</span>
          <span style="font-size:0.78rem;color:var(--danger)">⚠ ${escHtml(String(d.error))}</span>
        </div>`;
      }
      const c = (d?.checks || []).find(x => x.id === cid);
      if (!c) {
        return `<div style="display:flex;align-items:flex-start;gap:10px;padding:6px 0;border-bottom:1px solid var(--border)">
          <span style="flex:0 0 110px;font-weight:500;font-size:0.82rem">${escHtml(botLabel(bot))}</span>
          <span style="font-size:0.78rem;color:var(--text3)">— No data</span>
        </div>`;
      }
      const fixHtml = c.fix
        ? `<div style="font-size:0.75rem;color:var(--text2);margin-top:3px">
             Fix: ${c.fix_page ? `<a href="#" onclick="event.preventDefault();nav(document.querySelector('[data-page=\\'${escHtml(c.fix_page)}\\']'))" style="color:var(--blue);text-decoration:none">${escHtml(c.fix)}</a>` : escHtml(c.fix)}
           </div>`
        : '';
      return `<div style="display:flex;align-items:flex-start;gap:10px;padding:6px 0;border-bottom:1px solid var(--border)">
        <span style="flex:0 0 110px;font-weight:500;font-size:0.82rem">${escHtml(botLabel(bot))}</span>
        <span style="flex-shrink:0;font-size:1rem">${c.icon}</span>
        <div style="flex:1;min-width:0">
          <div style="font-size:0.8rem;color:var(--text2)">${escHtml(c.detail)}</div>
          ${fixHtml}
        </div>
        <span style="font-size:0.65rem;color:var(--text3);flex-shrink:0;text-transform:uppercase;letter-spacing:.04em;padding-top:2px">${escHtml(c.severity)}</span>
      </div>`;
    }).join('');

    const detailRow = isOpen
      ? `<tr><td colspan="${colCount}" style="background:var(--bg2);padding:10px 14px">${detailItems}</td></tr>`
      : '';

    return `
      <tr onclick="_healthToggleRow('${cid}')" style="cursor:pointer">
        <td style="font-size:0.82rem;color:var(--text2)">
          <span style="display:inline-block;width:14px;color:var(--text3)">${chev}</span> ${escHtml(label)}
        </td>
        ${cols}
      </tr>
      ${detailRow}
    `;
  }).join('');

  const issueNote = issues.length
    ? `<div style="font-size:0.82rem;color:var(--yellow);margin-top:10px">⚠ ${issues.length} issue${issues.length>1?'s':''} found.</div>`
    : `<div style="font-size:0.82rem;color:var(--green);margin-top:10px">✅ All checks passing.</div>`;

  const hint = `<div style="font-size:0.72rem;color:var(--text3);margin-bottom:8px">Click any row for per-bot details and fixes.</div>`;

  return `<div class="card" style="margin-bottom:16px">
    <div class="card-title" style="margin-bottom:6px">All Bots — Summary</div>
    ${hint}
    <table style="width:auto">
      <thead><tr><th></th>${headerCols}</tr></thead>
      <tbody>${rows}</tbody>
    </table>
    ${issueNote}
  </div>`;
}

function renderConfigHealth() {
  const el = document.getElementById('config-health-panel');
  // Bot list comes from the canonical pod state — bots without health data
  // yet still appear in the table, falling through to em-dash cells.
  const canonicalBots = orderedBotIds(
    (_statusData && _statusData.bots) || (_networkData && _networkData.bots)
  );
  if (!canonicalBots.length) {
    el.innerHTML = '<div class="empty" style="padding:20px 0">No bots in this pod yet.</div>';
    return;
  }
  const data = _healthData || {};
  const botEntries = canonicalBots.map(b => [b, data[b] || {}]);
  el.innerHTML = _healthAllSummary(botEntries);
}

